"""
Random quiz generator on LangGraph.

Input:  QuizRequest (Pydantic) -> free-text prompt like
        "Which of the following is a valid AWS VPC subnet?"
Output: Quiz (Pydantic)

Graph:
  parse_prompt -> plan_slots -> [generate_question x N, parallel] -> review
                                      ^                               |
                                     +---- retry only the bad ones --+
                                                                     |
                                                                  assemble

Install:  pip install langgraph langchain langchain-anthropic pydantic
Run:      python quiz_graph.py "medium quiz on AWS VPC networking" -n 6
Model:    set QUIZ_MODEL (default anthropic:claude-sonnet-5-5), needs ANTHROPIC_API_KEY
"""

from __future__ import annotations

import argparse
import os
import random
from typing import Annotated, Literal, TypedDict

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field, model_validator

from app.generation import model_settings

QuestionType = Literal["multiple_choice", "true_false", "short_answer"]
Difficulty = Literal["easy", "medium", "hard"]


# --------------------------------------------------------------------------
# Pydantic models
# --------------------------------------------------------------------------
class QuizRequest(BaseModel):
    """What the user hands in."""

    prompt: str = Field(..., description="Free text: topic, type of quiz, anything extra")
    num_questions: int = Field(5, ge=1, le=30, description="Default if the prompt doesn't say")
    seed: int | None = Field(None, description="Set for a reproducible plan; None = random")
    max_retries: int = Field(2, ge=0, le=5)
    allowed_types: list[QuestionType] | None = Field(
        None, description="Restrict question types; None = any"
    )
    age: int | None = Field(
        None, ge=5, le=99, description="Learner's age; questions match their level"
    )
    source: str | None = Field(None, description="Lesson text the questions must be based on")


class QuizSpec(BaseModel):
    """The prompt, parsed into something the graph can act on."""

    topic: str
    difficulty: Difficulty = "medium"
    question_types: list[QuestionType] = Field(default_factory=lambda: ["multiple_choice"])
    num_questions: int = Field(5, ge=1, le=30)
    style_notes: str = Field("", description="Tone, audience, scenario-based, etc.")


class Plan(BaseModel):
    subtopics: list[str] = Field(..., description="Distinct angles, one per question")


class Question(BaseModel):
    index: int = 0
    type: QuestionType
    question: str
    options: list[str] = Field(default_factory=list)  # multiple_choice only
    answer: str
    explanation: str

    @model_validator(mode="after")
    def _check(self):
        if self.type == "multiple_choice":
            if len(self.options) != 4:
                raise ValueError("multiple_choice needs exactly 4 options")
            if self.answer not in self.options:
                raise ValueError("answer must be one of the options")
        if self.type == "true_false":
            self.options = ["True", "False"]
            if self.answer not in self.options:
                raise ValueError("answer must be True or False")
        return self


class Quiz(BaseModel):
    title: str
    topic: str
    difficulty: Difficulty
    questions: list[Question]
    unresolved: list[int] = Field(
        default_factory=list, description="Indices that never passed review"
    )


class BadItem(BaseModel):
    index: int
    issue: str


class Review(BaseModel):
    bad: list[BadItem] = Field(default_factory=list)


class Slot(BaseModel):
    index: int
    subtopic: str
    qtype: QuestionType
    difficulty: Difficulty


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------
def merge_by_index(a: dict[int, Question], b: dict[int, Question]) -> dict[int, Question]:
    """Parallel workers write one question each; retries overwrite the old one."""
    return {**(a or {}), **(b or {})}


class State(TypedDict, total=False):
    request: QuizRequest
    spec: QuizSpec
    slots: list[Slot]
    questions: Annotated[dict[int, Question], merge_by_index]
    bad: list[BadItem]
    attempt: int
    quiz: Quiz


load_dotenv()


def _llm(model: str | None = None, temperature: float = 1.0):
    """The chat model. `model` overrides QUIZ_MODEL (same format), e.g. for a separate visual agent."""
    runtime = model_settings.get()
    model = model or (
        runtime.quiz_model if runtime else os.getenv("QUIZ_MODEL", "anthropic:claude-sonnet-5-5")
    )
    timeout = (
        runtime.model_timeout_seconds if runtime else int(os.getenv("MODEL_TIMEOUT_SECONDS", "60"))
    )
    if model.startswith("openrouter:"):
        # e.g. openrouter:anthropic/claude-sonnet-5.5 (OpenAI-compatible endpoint)
        return init_chat_model(
            "openai:" + model.removeprefix("openrouter:"),
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
            temperature=temperature,
            timeout=timeout,
            max_retries=0,
        )
    return init_chat_model(
        model,
        temperature=temperature,
        timeout=timeout,
        max_retries=0,
    )


def _context(req: QuizRequest | None) -> str:
    """Extra prompt lines when the quiz is tied to a learner's age and/or a lesson."""
    if req is None:
        return ""
    parts = []
    if req.age:
        parts.append(
            f"Audience: a {req.age}-year-old learner. Match their vocabulary and reading level."
        )
    if req.source:
        parts.append(
            "Only test what this lesson teaches:\n<lesson>\n" + req.source[:8000] + "\n</lesson>"
        )
    return "\n\n" + "\n".join(parts) if parts else ""


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------
def parse_prompt(state: State) -> dict:
    req = state["request"]
    spec = (
        _llm()
        .with_structured_output(QuizSpec)
        .invoke(
            "Turn this quiz request into a spec. If the user doesn't state a number of "
            f"questions, use {req.num_questions}. If they don't state question types, "
            "use multiple_choice.\n\nRequest: " + req.prompt
        )
    )
    if req.allowed_types:
        # enforced in code so the model can't sneak in a type the caller can't render
        spec.question_types = [t for t in spec.question_types if t in req.allowed_types] or [
            "multiple_choice"
        ]
    return {"spec": spec, "attempt": 0, "questions": {}}


def plan_slots(state: State) -> dict:
    """Randomness lives here: LLM brainstorms angles, code shuffles and assigns types."""
    req, spec = state["request"], state["spec"]
    rng = random.Random(req.seed)
    plan = (
        _llm()
        .with_structured_output(Plan)
        .invoke(
            f"List {spec.num_questions * 2} distinct, non-overlapping subtopics or angles for a "
            f"{spec.difficulty} quiz on: {spec.topic}. Mix concepts, edge cases and practical scenarios."
            + _context(req)
        )
    )
    angles = list(dict.fromkeys(plan.subtopics))
    rng.shuffle(angles)
    angles = angles[: spec.num_questions]

    ladder = ["easy", "medium", "hard"]
    slots = []
    for i, angle in enumerate(angles):
        # difficulty drifts +-1 step around the requested level
        base = ladder.index(spec.difficulty)
        diff = ladder[max(0, min(2, base + rng.choice([-1, 0, 0, 1])))]
        slots.append(
            Slot(index=i, subtopic=angle, qtype=rng.choice(spec.question_types), difficulty=diff)
        )
    return {"slots": slots}


def fan_out(state: State):
    return [
        Send(
            "generate_question",
            {"slot": s, "spec": state["spec"], "request": state["request"], "feedback": ""},
        )
        for s in state["slots"]
    ]


def generate_question(payload: dict) -> dict:
    slot: Slot = payload["slot"]
    spec: QuizSpec = payload["spec"]
    fb = payload.get("feedback", "")
    q = (
        _llm()
        .with_structured_output(Question)
        .invoke(
            f"Write ONE {slot.qtype} quiz question.\n"
            f"Topic: {spec.topic}\nAngle: {slot.subtopic}\nDifficulty: {slot.difficulty}\n"
            f"Style notes: {spec.style_notes or 'none'}\n"
            "Rules: exactly one correct answer; for multiple_choice give 4 plausible options and "
            "set `answer` to the exact text of the correct option; explanation is 1-2 sentences."
            + (f"\nFix this problem from the last attempt: {fb}" if fb else "")
            + _context(payload.get("request"))
        )
    )
    q.index = slot.index
    if q.type == "multiple_choice":
        # models list the correct option first (measured: 12 of 12), so shuffle in code
        random.shuffle(q.options)
    return {"questions": {slot.index: q}}


def review(state: State) -> dict:
    req = state["request"]
    qs = [state["questions"][i] for i in sorted(state["questions"])]
    listing = "\n\n".join(f"[{q.index}] {q.model_dump_json()}" for q in qs)
    extra = _context(req)
    result = (
        _llm()
        .with_structured_output(Review)
        .invoke(
            "You are checking quiz questions. Flag a question only if the marked answer is wrong, "
            "more than one option is correct, the question is ambiguous, or it duplicates another"
            + (
                ", or if the lesson doesn't cover it or it doesn't suit the audience"
                if extra
                else ""
            )
            + ". Return an empty list if all are fine.\n\n"
            + listing
            + extra
        )
    )
    return {"bad": result.bad, "attempt": state.get("attempt", 0) + 1}


def route_after_review(state: State):
    if state["bad"] and state["attempt"] <= state["request"].max_retries:
        slots = {s.index: s for s in state["slots"]}
        return [
            Send(
                "generate_question",
                {
                    "slot": slots[b.index],
                    "spec": state["spec"],
                    "request": state["request"],
                    "feedback": b.issue,
                },
            )
            for b in state["bad"]
            if b.index in slots
        ]
    return "assemble"


def assemble(state: State) -> dict:
    spec = state["spec"]
    qs = [state["questions"][i] for i in sorted(state["questions"])]
    return {
        "quiz": Quiz(
            title=f"{spec.topic} ({spec.difficulty})",
            topic=spec.topic,
            difficulty=spec.difficulty,
            questions=qs,
            unresolved=[b.index for b in state.get("bad", [])],
        )
    }


# --------------------------------------------------------------------------
# Graph
# --------------------------------------------------------------------------
def build_graph():
    g = StateGraph(State)
    g.add_node("parse_prompt", parse_prompt)
    g.add_node("plan_slots", plan_slots)
    g.add_node("generate_question", generate_question)
    g.add_node("review", review)
    g.add_node("assemble", assemble)

    g.add_edge(START, "parse_prompt")
    g.add_edge("parse_prompt", "plan_slots")
    g.add_conditional_edges("plan_slots", fan_out, ["generate_question"])
    g.add_edge("generate_question", "review")
    g.add_conditional_edges("review", route_after_review, ["generate_question", "assemble"])
    g.add_edge("assemble", END)
    return g.compile()


def make_quiz(request: QuizRequest) -> Quiz:
    return build_graph().invoke({"request": request})["quiz"]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", help='e.g. "hard quiz on Apex governor limits, mostly MCQ"')
    ap.add_argument("-n", "--num", type=int, default=5)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    quiz = make_quiz(QuizRequest(prompt=args.prompt, num_questions=args.num, seed=args.seed))

    print(f"\n=== {quiz.title} ===\n")
    for q in quiz.questions:
        print(f"{q.index + 1}. [{q.type}] {q.question}")
        for o, letter in zip(q.options, "ABCD"):
            print(f"   {letter}) {o}")
        print(f"   -> {q.answer}  ({q.explanation})\n")
    if quiz.unresolved:
        print(f"Warning: questions {quiz.unresolved} still flagged after retries.")
