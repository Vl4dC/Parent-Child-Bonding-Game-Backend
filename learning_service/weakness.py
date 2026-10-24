"""
Weakness harness: after a quiz, build a follow-up on what the learner got wrong, with
*different* questions, so it tests understanding rather than memory of the old answers.

  diagnose      (1 model call) groups the misses into the ideas not yet understood and says what each
                wrong pick suggests the learner believes
  plan          (code) gives every new question an idea, an angle ("new example", "reverse it",
                "spot the mistake"...), a type, and for true/false whether the statement is true
  make_variant  (parallel, one model call per new question) writes it; code rejects copies: same
                correct answer, reused options, a near-identical question, wrong truth value; the
                writer is retried with the reason. A missed diagram question instead hides a
                *different* box of the same diagram.
  review        (1 model call) checks the answer keys; flagged questions are rewritten once, then dropped
  assemble      (code) drops near-duplicates, numbers the questions, lists what is being practised

        diagnose -> plan -> [make_variant x N] -> review -> assemble
                                ^                    |
                                +-- rewrite flagged --+

Answer positions are shuffled in code: models put the correct option first almost every time.
"""

from __future__ import annotations

import logging
import random
import re
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field

from learning_service.lesson import reading_level
from learning_service.quiz_graph import Question, QuestionType, _llm
from learning_service.safety import content_rules
from learning_service.visual_recall import (
    IllustratedQuestion,
    make_blank_items,
    to_mcq,
    to_true_false,
)

log = logging.getLogger("weakness")

MAX_WEAKNESSES = 5
MAX_QUESTIONS = 10
MAX_DIAGRAM_VARIANTS = 2
MAX_ATTEMPTS = 3  # tries per new question before giving up on it
REVIEW_ROUNDS = 1  # times flagged questions are rewritten
SIMILAR = 0.5  # content-word overlap above which two questions count as the same question
LESSON_CHARS = 8000

# Each new question on the same idea takes a different one of these.
ANGLES = [
    "a new everyday example or situation that uses the idea",
    "the idea asked the other way round (start from the result and ask for the cause)",
    "spot the mistake: which statement wrongly describes it",
    "what would change if one thing in the situation were different",
    "compare it with the idea it is most often confused with",
]

# A true/false statement that announces its own verdict ("...This is a mistake: ...", "True or false?")
GIVES_IT_AWAY = re.compile(
    r"\b(true or false|(this|that|it) is (a |an )?(mistake|incorrect|correct|wrong|right|true|false)|the answer is)\b",
    re.I,
)

STOPWORDS = {
    "the",
    "a",
    "an",
    "of",
    "to",
    "in",
    "is",
    "are",
    "and",
    "or",
    "what",
    "which",
    "how",
    "why",
    "does",
    "do",
    "it",
    "its",
    "that",
    "this",
    "for",
    "on",
    "with",
    "as",
    "at",
    "by",
    "be",
}


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class Miss(BaseModel):
    """One wrong answer, with what the learner picked."""

    index: int  # question index in the quiz they took
    question: IllustratedQuestion
    picked: str


class Weakness(BaseModel):
    concept: str = Field(
        ..., description="The underlying idea, a few words, in the learner's vocabulary"
    )
    misconception: str = Field(
        "",
        description="One sentence: what the wrong choice suggests they believe; "
        "empty if it looks like a guess",
    )
    misses: list[int] = Field(
        ..., description="The [numbers] of the missed questions this explains"
    )


class Diagnosis(BaseModel):
    weaknesses: list[Weakness]


class RemedialRequest(BaseModel):
    topic: str
    age: int = Field(..., ge=5, le=99)
    lesson_text: str
    misses: list[Miss] = Field(..., min_length=1)
    num_questions: int | None = Field(
        None, ge=1, le=MAX_QUESTIONS, description="None = decide from the weaknesses"
    )
    seed: int | None = None


class Slot(BaseModel):
    n: int
    weakness: int  # index into the weaknesses list
    angle: str
    qtype: QuestionType
    truth: bool  # for true_false: should the statement be true?
    diagram_miss: int | None = (
        None  # Miss.index of a diagram question to vary by hiding another box
    )


class BadVariant(BaseModel):
    index: int  # Slot.n
    issue: str


class VariantReview(BaseModel):
    bad: list[BadVariant] = Field(default_factory=list)


class RemedialQuiz(BaseModel):
    focus: list[str]  # what is being practised, for the learner to see
    questions: list[IllustratedQuestion]


# --------------------------------------------------------------------------
# Pure helpers (all the "don't just repeat the old question" rules live here)
# --------------------------------------------------------------------------
def content_words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.casefold())) - STOPWORDS


def similar(a: str, b: str) -> float:
    """Share of content words two texts have in common (0..1)."""
    wa, wb = content_words(a), content_words(b)
    return len(wa & wb) / len(wa | wb) if wa | wb else 0.0


def is_diagram(q: IllustratedQuestion) -> bool:
    return bool(q.visual and q.answer_visual and q.node_id)


def normalize(raw: list[Weakness], misses: list[Miss]) -> list[Weakness]:
    """Trust the model's grouping only where it makes sense: known indices, each miss in one weakness,
    and every miss accounted for. Most-missed idea first."""
    valid = {m.index for m in misses}
    seen: set[int] = set()
    out: list[Weakness] = []
    for w in raw:
        idx = [i for i in dict.fromkeys(w.misses) if i in valid and i not in seen]
        if not idx or not w.concept.strip():
            continue
        seen.update(idx)
        out.append(
            Weakness(
                concept=w.concept.strip()[:80], misconception=w.misconception.strip(), misses=idx
            )
        )
    for m in misses:
        if m.index not in seen:  # the model skipped it: the explanation names the idea well enough
            out.append(
                Weakness(
                    concept=m.question.explanation.strip()[:80], misconception="", misses=[m.index]
                )
            )
    out.sort(key=lambda w: -len(w.misses))
    return out[:MAX_WEAKNESSES]


def plan_slots(req: RemedialRequest, weaknesses: list[Weakness], rng: random.Random) -> list[Slot]:
    total = req.num_questions or max(3, min(MAX_QUESTIONS, 2 * len(weaknesses)))
    sequence = [k % len(weaknesses) for k in range(total)]  # round-robin, most-missed idea first
    starts = {w: rng.randrange(len(ANGLES)) for w in range(len(weaknesses))}
    used: dict[int, int] = {}
    slots = []
    for n, w in enumerate(sequence):
        j = used[w] = used.get(w, -1) + 1
        slots.append(
            Slot(
                n=n,
                weakness=w,
                angle=ANGLES[(starts[w] + j) % len(ANGLES)],
                qtype=rng.choices(["multiple_choice", "true_false"], weights=[7, 3])[0],
                truth=rng.random() < 0.5,
            )
        )
    by_index = {m.index: m for m in req.misses}
    converted = 0
    for w, weakness in enumerate(weaknesses):
        if converted >= MAX_DIAGRAM_VARIANTS:
            break
        miss = next(
            (by_index[i] for i in weakness.misses if is_diagram(by_index[i].question)), None
        )
        slot = next((s for s in slots if s.weakness == w), None)
        if miss and slot:
            slot.diagram_miss = miss.index
            converted += 1
    return slots


def problem_with(q: Question, slot: Slot, originals: list[Miss]) -> str | None:
    """Why a freshly written question is unacceptable, or None. The text goes back to the writer."""
    if q.type != slot.qtype:
        return f"it must be a {slot.qtype} question, not {q.type}"
    if q.type == "true_false" and (q.answer == "True") != slot.truth:
        return f"the statement must be {'true' if slot.truth else 'false'}, so `answer` is {slot.truth}"
    if q.type == "true_false" and GIVES_IT_AWAY.search(q.question):
        return (
            "the statement gives away its own answer; write only the plain claim, without 'true or false' "
            "and without saying it is right, wrong or a mistake"
        )
    for m in originals:
        old = m.question
        if is_diagram(old):
            continue
        if similar(q.question, old.question) > SIMILAR:
            return "it is too similar to the question they missed; change the situation and the wording"
        if q.type == "multiple_choice" and old.type == "multiple_choice":
            if q.answer.casefold() == old.answer.casefold():
                return "its correct answer repeats the old one; ask about the idea from a direction where the answer differs"
            if len({o.casefold() for o in q.options} & {o.casefold() for o in old.options}) >= 2:
                return "it reuses options from the old question; write new options"
    return None


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------
def describe_miss(m: Miss) -> str:
    q = m.question
    if is_diagram(q):
        return f"- A diagram question: the hidden box was “{q.answer}”. {q.explanation} (they chose “{m.picked}”)"
    return f"- “{q.question}” (correct: {q.answer}; they chose: {m.picked})"


def diagnose_prompt(req: RemedialRequest) -> str:
    listing = "\n\n".join(
        f"[{m.index}] {m.question.question}\n    Options: {m.question.options}\n    Correct: {m.question.answer}\n"
        f"    They chose: {m.picked}\n    Why: {m.question.explanation}"
        for m in req.misses
    )
    return (
        f'A {req.age}-year-old studied "{req.topic}" and took a quiz. These are the questions they got wrong:\n\n'
        f"{listing}\n\n"
        "Group the misses into the underlying ideas they have not mastered (one weakness can explain several "
        'misses). For each: `concept` (a few words in the learner\'s vocabulary, e.g. "How clouds form"), '
        "`misconception` (one sentence: what their wrong choice suggests they believe; leave empty if it looks "
        "like a guess) and `misses` (the [numbers] it explains). Every missed question belongs to exactly one weakness.\n"
        f"The concepts are shown to the learner.\n{content_rules(req.age)}"
    )


def variant_prompt(
    req: RemedialRequest, slot: Slot, weakness: Weakness, originals: list[Miss], feedback: str
) -> str:
    if slot.qtype == "true_false":
        kind = (
            f"a true_false question: one statement that is {'TRUE' if slot.truth else 'FALSE'} "
            f'(set `answer` to "{"True" if slot.truth else "False"}"). Write only the plain claim: never '
            "'true or false', and never say whether it is right, wrong or a mistake"
        )
    else:
        kind = (
            "a multiple_choice question with exactly 4 plausible options and one correct one (set `answer` to "
            "the exact text of the correct option); make one wrong option the tempting belief described above"
        )
    return (
        f'A {req.age}-year-old studied "{req.topic}", then got quiz questions wrong. Write ONE new question to '
        "check whether they understand the idea now.\n\n"
        f"Idea they are weak on: {weakness.concept}\n"
        f"What they seem to believe: {weakness.misconception or 'unclear'}\n"
        "Questions they missed (do NOT reuse these):\n"
        + "\n".join(describe_miss(m) for m in originals)
        + "\n\n"
        "Make it genuinely different, so it can't be answered by remembering the old question:\n"
        f"- Angle: {slot.angle}.\n"
        "- A new situation and new wording. Don't reuse the old answer text or the old wrong options.\n"
        "- Test the idea itself, not the old fact.\n"
        f"Question type: {kind}.\n"
        "Exactly one correct answer; the explanation is 1-2 sentences that clear up the misunderstanding.\n"
        f"Write for them: {reading_level(req.age)}\n"
        f"{content_rules(req.age)}"
        f"Only test what this lesson teaches:\n<lesson>\n{req.lesson_text[:LESSON_CHARS]}\n</lesson>"
        + (f"\n\n{feedback}" if feedback else "")
    )


# --------------------------------------------------------------------------
# Graph
# --------------------------------------------------------------------------
def merge_by_slot(a: dict[int, IllustratedQuestion], b: dict[int, IllustratedQuestion]):
    """Parallel writers add one question each; a rewrite replaces the old one."""
    return {**(a or {}), **(b or {})}


class State(TypedDict, total=False):
    request: RemedialRequest
    weaknesses: list[Weakness]
    slots: list[Slot]
    questions: Annotated[dict[int, IllustratedQuestion], merge_by_slot]
    bad: list[BadVariant]
    attempt: int
    result: RemedialQuiz


def diagnose(state: State) -> dict:
    req = state["request"]
    try:
        raw = _llm().with_structured_output(Diagnosis).invoke(diagnose_prompt(req)).weaknesses
    except Exception:
        log.warning("diagnosis failed; treating each miss as its own weakness")
        raw = []
    return {"weaknesses": normalize(raw, req.misses), "attempt": 0, "questions": {}}


def plan(state: State) -> dict:
    req = state["request"]
    return {"slots": plan_slots(req, state["weaknesses"], random.Random(req.seed))}


def fan_out(state: State):
    return [
        Send(
            "make_variant",
            {
                "request": state["request"],
                "slot": s,
                "feedback": "",
                "weakness": state["weaknesses"][s.weakness],
            },
        )
        for s in state["slots"]
    ]


def diagram_variant(
    req: RemedialRequest, slot: Slot, miss: Miss, rng: random.Random
) -> IllustratedQuestion | None:
    """Same diagram as the missed question, a different box hidden."""
    old = miss.question
    try:
        items = make_blank_items(
            old.answer_visual, req.topic, req.age, 1, exclude=frozenset({old.node_id})
        )
    except Exception:
        log.warning("diagram variant failed; falling back to a text question")
        return None
    if not items:
        return None
    return (to_mcq if slot.qtype == "multiple_choice" else to_true_false)(items[0], rng)


def text_variant(
    req: RemedialRequest,
    slot: Slot,
    weakness: Weakness,
    originals: list[Miss],
    feedback: str,
    rng: random.Random,
) -> IllustratedQuestion | None:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            q = (
                _llm()
                .with_structured_output(Question)
                .invoke(variant_prompt(req, slot, weakness, originals, feedback))
            )
            problem = problem_with(q, slot, originals)
        except (
            Exception
        ):  # network errors, or the model returning something Question itself rejects
            log.info("variant attempt %d failed", attempt)
            problem = "The previous output was invalid. Return the requested structured question."
        if problem is None:
            if q.type == "multiple_choice":
                rng.shuffle(q.options)
            return IllustratedQuestion(**q.model_dump())
        log.info("variant rejected (attempt %d)", attempt)
        feedback = f"Your previous attempt was rejected: {problem}. Write a different question that fixes this."
    return None


def make_variant(payload: dict) -> dict:
    req: RemedialRequest = payload["request"]
    slot: Slot = payload["slot"]
    weakness: Weakness = payload["weakness"]
    feedback: str = payload.get("feedback", "")
    rng = random.Random(None if req.seed is None else req.seed * 1000 + slot.n)
    by_index = {m.index: m for m in req.misses}
    q = None
    if slot.diagram_miss is not None and not feedback:
        q = diagram_variant(req, slot, by_index[slot.diagram_miss], rng)
    if q is None:
        q = text_variant(req, slot, weakness, [by_index[i] for i in weakness.misses], feedback, rng)
    return {"questions": {slot.n: q} if q else {}}


def review(state: State) -> dict:
    """Second pair of eyes on the answer keys of the written (non-diagram) questions."""
    req = state["request"]
    written = {n: q for n, q in state["questions"].items() if not q.visual}
    bad: list[BadVariant] = []
    if written:
        listing = "\n\n".join(
            f"[{n}] "
            + q.model_dump_json(include={"type", "question", "options", "answer", "explanation"})
            for n, q in sorted(written.items())
        )
        result = (
            _llm()
            .with_structured_output(VariantReview)
            .invoke(
                "You are checking quiz questions. Flag a question only if the marked answer is wrong, more than one "
                "option is correct, it is ambiguous, or the lesson does not cover it. Return an empty list if all are "
                f"fine.\n\n{listing}\n\n<lesson>\n{req.lesson_text[:LESSON_CHARS]}\n</lesson>"
            )
        )
        bad = [b for b in result.bad if b.index in written]
    return {"bad": bad, "attempt": state.get("attempt", 0) + 1}


def route_after_review(state: State):
    if state["bad"] and state["attempt"] <= REVIEW_ROUNDS:
        slots = {s.n: s for s in state["slots"]}
        return [
            Send(
                "make_variant",
                {
                    "request": state["request"],
                    "slot": slots[b.index],
                    "weakness": state["weaknesses"][slots[b.index].weakness],
                    "feedback": f"A reviewer flagged your last version of this question: {b.issue}",
                },
            )
            for b in state["bad"]
            if b.index in slots
        ]
    return "assemble"


def assemble(state: State) -> dict:
    flagged = {
        b.index for b in state.get("bad", [])
    }  # still flagged after the rewrite: don't ship a doubtful key
    slots = {s.n: s for s in state["slots"]}
    kept: list[tuple[int, IllustratedQuestion]] = []
    for n in sorted(state["questions"]):
        q = state["questions"][n]
        if n in flagged:
            continue
        if any(
            similar(q.question, k.question) > SIMILAR
            for _, k in kept
            if not q.visual and not k.visual
        ):
            continue  # two new questions that ask the same thing
        kept.append((n, q))
    focus = list(dict.fromkeys(state["weaknesses"][slots[n].weakness].concept for n, _ in kept))
    return {
        "result": RemedialQuiz(
            focus=focus,
            questions=[q.model_copy(update={"index": i}) for i, (_, q) in enumerate(kept)],
        )
    }


def build_graph():
    g = StateGraph(State)
    g.add_node("diagnose", diagnose)
    g.add_node("plan", plan)
    g.add_node("make_variant", make_variant)
    g.add_node("review", review)
    g.add_node("assemble", assemble)
    g.add_edge(START, "diagnose")
    g.add_edge("diagnose", "plan")
    g.add_conditional_edges("plan", fan_out, ["make_variant"])
    g.add_edge("make_variant", "review")
    g.add_conditional_edges("review", route_after_review, ["make_variant", "assemble"])
    g.add_edge("assemble", END)
    return g.compile()


def make_remedial(request: RemedialRequest) -> RemedialQuiz:
    return build_graph().invoke({"request": request})["result"]
