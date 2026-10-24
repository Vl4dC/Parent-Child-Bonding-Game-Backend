"""
Visualization harness: draws the visuals a lesson needs.

Built the way Claude's own harness handles skills and artifacts:
  skills/*.md   frontmatter `description` = when to use it (planners see only these)
                body                       = how to draw it (loaded only for the chosen kind)
  plan          WHEN to draw is decided while the lesson is written: lesson.py gives each
                section a `visual` plan (or none), adapting to topic, age and preference.
                Only a learner's own request (`ask`) needs a separate planner call here.
  draw          per plan item, in parallel: load that skill, write the code, validate,
                and retry with the validator's reason if it was rejected
  render        render.document() wraps each visual in a sandboxed page

Graph:
  plan_visuals -> [draw_visual x N, parallel] -> assemble
  plan_visuals ----- nothing worth drawing ----> assemble

Run:  python -m uv run python -m visuals.harness "photosynthesis" --age 10 --ask "show the inputs and outputs"
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Annotated, TypedDict, get_args

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.generation import model_settings
from learning_service.lesson import reading_level
from learning_service.quiz_graph import _llm
from learning_service.safety import content_rules
from learning_service.visuals import render
from learning_service.visuals.models import (
    Visual,
    VisualDraft,
    VisualKind,
    VisualPlan,
    VisualPlanItem,
    VisualRequest,
)
from learning_service.visuals.skillbook import SKILLS, menu

log = logging.getLogger("visuals")
MAX_ATTEMPTS = 3


def drawer_llm():
    """The model that writes the visual code. VISUAL_MODEL (same format as QUIZ_MODEL, e.g.
    `openrouter:anthropic/claude-haiku-5.5`) if set, else the main model. Planning stays on the main model."""
    runtime = model_settings.get()
    model = runtime.visual_model if runtime else os.getenv("VISUAL_MODEL")
    return _llm(model) if model else _llm()


assert set(SKILLS) == set(get_args(VisualKind)), (
    f"skills/ must define exactly {get_args(VisualKind)}"
)


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------
def merge_by_index(a: dict[int, Visual], b: dict[int, Visual]) -> dict[int, Visual]:
    return {**(a or {}), **(b or {})}


class State(TypedDict, total=False):
    request: VisualRequest
    plan: list[VisualPlanItem]
    drawn: Annotated[dict[int, Visual], merge_by_index]
    visuals: list[Visual]


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------
def _sections(req: VisualRequest) -> str:
    return "\n\n".join(f"[{i}] {s.heading}\n{s.body}" for i, s in enumerate(req.lesson.sections))


def plan_prompt(req: VisualRequest) -> str:
    """Planner for a learner's own request (`ask`); lesson-planned visuals skip this."""
    return (
        f'A {req.age}-year-old learner studying "{req.topic}" asked to see: "{req.ask}".\n'
        "Plan exactly one visual that answers this request, tied to the closest lesson section. "
        "Show the real mechanism, not a decorative picture.\n\n"
        f"Available kinds (pick the one whose description fits best):\n{menu()}\n\n"
        f"Lesson sections (refer to them by number):\n{_sections(req)}\n\n"
        "`brief` must say exactly what to draw: the elements, their labels in the learner's words, "
        "and for an interactive, the control and what it changes.\n\n"
        f"{content_rules(req.age)}"
    )


def lesson_plan(req: VisualRequest) -> list[VisualPlanItem]:
    """The visuals the lesson writer planned, section by section."""
    return [
        VisualPlanItem(section_index=i, **s.visual.model_dump())
        for i, s in enumerate(req.lesson.sections)
        if s.visual
    ]


def plan_visuals(state: State) -> dict:
    req = state["request"]
    if req.ask is None:
        return {"plan": lesson_plan(req)[: req.max_visuals], "drawn": {}}
    plan = _llm().with_structured_output(VisualPlan).invoke(plan_prompt(req))
    last = len(req.lesson.sections) - 1
    items = [
        it.model_copy(update={"section_index": min(max(it.section_index, 0), last)})
        for it in plan.items
    ]
    return {"plan": items[:1], "drawn": {}}


def fan_out(state: State):
    if not state["plan"]:
        return "assemble"
    return [
        Send("draw_visual", {"request": state["request"], "item": it, "n": n})
        for n, it in enumerate(state["plan"])
    ]


def draw_prompt(req: VisualRequest, item: VisualPlanItem, feedback: str = "") -> str:
    section = req.lesson.sections[item.section_index]
    return (
        f"{SKILLS[item.kind].body}\n\n---\n"
        f"Learner: {req.age} years old. Write for them: {reading_level(req.age)}\n"
        f"{content_rules(req.age)}"
        f"Topic: {req.topic}\n"
        f"Lesson section: {section.heading}\n{section.body}\n\n"
        f"Purpose: {item.purpose}\n"
        f"What to draw: {item.brief}\n"
        + (f"\n{feedback}\n" if feedback else "")
        + "\nReturn title, caption, alt and code."
    )


def draw_visual(payload: dict) -> dict:
    req: VisualRequest = payload["request"]
    item: VisualPlanItem = payload["item"]
    feedback = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            draft = (
                drawer_llm()
                .with_structured_output(VisualDraft)
                .invoke(draw_prompt(req, item, feedback))
            )
        except Exception:
            log.warning("drawing %s visual failed", item.kind)
            return {"drawn": {}}
        code = render.clean(draft.code)
        problem = render.validate(item.kind, code)
        if problem is None:
            visual = Visual(
                **draft.model_dump(exclude={"code"}),
                code=code,
                kind=item.kind,
                section_index=item.section_index,
            )
            return {"drawn": {payload["n"]: visual}}
        log.info("%s visual rejected (attempt %d)", item.kind, attempt)
        feedback = f"Your previous attempt was rejected: {problem}. Fix that.\nPrevious code:\n{code[:4000]}"
    return {"drawn": {}}  # never passed validation: drop it rather than show something broken


def assemble(state: State) -> dict:
    drawn = state.get("drawn", {})
    return {"visuals": [drawn[n] for n in sorted(drawn)]}


# --------------------------------------------------------------------------
# Graph
# --------------------------------------------------------------------------
def build_graph():
    g = StateGraph(State)
    g.add_node("plan_visuals", plan_visuals)
    g.add_node("draw_visual", draw_visual)
    g.add_node("assemble", assemble)
    g.add_edge(START, "plan_visuals")
    g.add_conditional_edges("plan_visuals", fan_out, ["draw_visual", "assemble"])
    g.add_edge("draw_visual", "assemble")
    g.add_edge("assemble", END)
    return g.compile()


def make_visuals(request: VisualRequest) -> list[Visual]:
    return build_graph().invoke({"request": request})["visuals"]


if __name__ == "__main__":
    from learning_service.lesson import LessonRequest, make_lesson

    ap = argparse.ArgumentParser()
    ap.add_argument("topic")
    ap.add_argument("--age", type=int, required=True)
    ap.add_argument("--ask", default=None)
    ap.add_argument("--out", default="visuals_preview.html")
    args = ap.parse_args()

    lesson = make_lesson(LessonRequest(topic=args.topic, age=args.age))
    visuals = make_visuals(
        VisualRequest(topic=args.topic, age=args.age, lesson=lesson, ask=args.ask)
    )
    frames = "".join(
        f"<h2>{v.title} <small>({v.kind}, section {v.section_index})</small></h2><p>{v.caption}</p>"
        f'<iframe sandbox="allow-scripts" style="width:100%;height:420px;border:1px solid #ccc" '
        f'srcdoc="{render.html.escape(render.document(v, str(i)))}"></iframe>'
        for i, v in enumerate(visuals)
    )
    Path(args.out).write_text(
        f"<!doctype html><meta charset=utf-8><h1>{lesson.title}</h1>{frames or 'No visuals planned.'}",
        encoding="utf-8",
    )
    print(f"{len(visuals)} visual(s) -> {args.out}")
