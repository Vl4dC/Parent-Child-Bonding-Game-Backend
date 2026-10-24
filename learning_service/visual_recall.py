"""
Visual recall: turn the lesson's diagrams into questions by hiding one box.

The model only *chooses* which box is worth recalling and writes believable wrong
answers; code does the hiding, so the picture is always the real diagram minus one
label, and the hidden word can't leak elsewhere in it.

  recallable(code)        -> boxes in a Mermaid flowchart that can be hidden fairly
  make_blank_items(...)   -> model picks boxes + distractors, validated in code
  hidden(item, fill)      -> the diagram with that box shown as "?" (or a candidate label)
  to_mcq / to_true_false  -> quiz questions ("what goes in the box?" / "is this box right?")
"""

from __future__ import annotations

import logging
import random
import re

from pydantic import BaseModel, Field

from learning_service.lesson import reading_level
from learning_service.quiz_graph import Question, _llm
from learning_service.visuals.models import Visual

log = logging.getLogger("visual_recall")

# A node definition with a quoted label: id["..."], id("..."), id{"..."}, id(["..."]), id>"..."] ...
# Edge labels (|"..."|) never match: their quote isn't preceded by `id` + an opening bracket.
NODE = re.compile(
    r'(?<![\w|"])(?P<id>[A-Za-z_]\w*)\s*(?P<open>\(\[|\[\(|\(\(|\[\[|\{\{|\[/|\[\\|\[|\(|\{|>)\s*"(?P<label>[^"\n]+)"'
)
BLANK = "?"
MAX_LABEL = 60


class Node(BaseModel):
    id: str
    label: str


def is_flowchart(code: str) -> bool:
    first = code.strip().split(maxsplit=1)
    return bool(first) and first[0] in ("flowchart", "graph")


def nodes(code: str) -> list[Node]:
    """Labelled boxes of a flowchart, first definition of each id wins."""
    if not is_flowchart(code):
        return []
    seen: dict[str, str] = {}
    for line in code.splitlines():
        if line.strip().startswith("subgraph"):
            continue
        for m in NODE.finditer(line):
            seen.setdefault(m["id"], m["label"].strip())
    return [Node(id=k, label=v) for k, v in seen.items()]


def blank_code(code: str, node_id: str, fill: str = BLANK, highlight: bool = True) -> str:
    """Replace every label definition of `node_id` with `fill`; dash its border so it stands out."""
    fill = fill.replace('"', "'")

    def sub(m: re.Match) -> str:
        if m["id"] != node_id:
            return m.group(0)
        return m.group(0).replace(f'"{m["label"]}"', f'"{fill}"', 1)

    lines = [
        ln if ln.strip().startswith("subgraph") else NODE.sub(sub, ln) for ln in code.splitlines()
    ]
    out = "\n".join(lines)
    return out + f"\n  style {node_id} stroke-width:3px,stroke-dasharray:6 4" if highlight else out


def recallable(code: str) -> list[Node]:
    """Boxes that can be hidden fairly: unique label that appears nowhere else in the diagram."""
    all_nodes = nodes(code)
    labels = [n.label.casefold() for n in all_nodes]
    fair = []
    for n in all_nodes:
        if len(n.label) < 2 or labels.count(n.label.casefold()) > 1:
            continue
        if n.label.casefold() in blank_code(code, n.id, "", highlight=False).casefold():
            continue  # the answer would still be readable somewhere else (edge label, other box)
        fair.append(n)
    return fair


# --------------------------------------------------------------------------
# Model: choose boxes and write distractors
# --------------------------------------------------------------------------
class BlankChoice(BaseModel):
    node_id: str = Field(..., description="id of the box to hide")
    distractors: list[str] = Field(
        ..., description="3 believable but wrong labels, same style and length"
    )
    explanation: str = Field(
        ..., description="One sentence: why the real label belongs in that box"
    )


class BlankPlan(BaseModel):
    items: list[BlankChoice] = Field(default_factory=list)


class BlankItem(BaseModel):
    """One hideable box, ready to become a question or a flashcard."""

    visual: Visual
    node_id: str
    answer: str
    distractors: list[str]  # exactly 3
    explanation: str


def blank_prompt(visual: Visual, candidates: list[Node], topic: str, age: int, k: int) -> str:
    listing = "\n".join(f"- {n.id}: {n.label}" for n in candidates)
    return (
        f'A {age}-year-old just studied "{topic}" with this diagram (Mermaid):\n{visual.code}\n\n'
        f"Pick the {k} box(es) most worth recalling - core ideas from the lesson, not trivial ones like "
        f"'Start'. Choose only from these ids:\n{listing}\n\n"
        "For each, write 3 distractors: labels that look like they could belong in that box (same style "
        "and length as the real one) and would tempt someone who half-learned it, but are clearly wrong "
        "to someone who learned it. Never reuse a label that is already in the diagram. "
        f"Write for the learner: {reading_level(age)}. Add a one-sentence explanation of why the real "
        "label belongs there."
    )


def _clean_distractors(raw: list[str], taken: set[str]) -> list[str]:
    out: list[str] = []
    for d in raw:
        d = d.strip().strip('"').strip()
        if d and len(d) <= MAX_LABEL and d.casefold() not in taken:
            taken.add(d.casefold())
            out.append(d)
    return out[:3]


def make_blank_items(
    visual: Visual, topic: str, age: int, k: int = 1, exclude: frozenset[str] = frozenset()
) -> list[BlankItem]:
    """`exclude`: node ids not to hide (e.g. the box a learner already missed, so a follow-up asks about another)."""
    candidates = [n for n in recallable(visual.code) if n.id not in exclude]
    if not candidates or k < 1:
        return []
    by_id = {n.id: n for n in candidates}
    plan = (
        _llm()
        .with_structured_output(BlankPlan)
        .invoke(blank_prompt(visual, candidates, topic, age, k))
    )
    diagram_labels = {n.label.casefold() for n in nodes(visual.code)}
    items: list[BlankItem] = []
    for choice in plan.items:
        node = by_id.get(choice.node_id)
        if node is None or any(i.node_id == node.id for i in items):
            continue
        distractors = _clean_distractors(choice.distractors, set(diagram_labels))
        if len(distractors) < 3:
            log.info("dropping blank %s: only %d usable distractors", node.id, len(distractors))
            continue
        items.append(
            BlankItem(
                visual=visual,
                node_id=node.id,
                answer=node.label,
                distractors=distractors,
                explanation=choice.explanation,
            )
        )
    return items[:k]


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------
class IllustratedQuestion(Question):
    """A quiz question that may come with a picture (and a completed picture shown after answering)."""

    visual: Visual | None = None
    answer_visual: Visual | None = None
    node_id: str | None = None  # which box was hidden, for diagram questions


def hidden(item: BlankItem, fill: str = BLANK) -> Visual:
    """The item's diagram with its box hidden (or filled with `fill`). Title/caption dropped: they may name it."""
    return item.visual.model_copy(
        update={
            "code": blank_code(item.visual.code, item.node_id, fill),
            "title": "Lesson diagram",
            "caption": "",
            "alt": "The lesson diagram with one box marked by a dashed border",
        }
    )


def to_mcq(item: BlankItem, rng: random.Random) -> IllustratedQuestion:
    options = [item.answer, *item.distractors]
    rng.shuffle(options)
    return IllustratedQuestion(
        type="multiple_choice",
        question=f"Look at the diagram. What belongs in the dashed box marked “{BLANK}”?",
        options=options,
        answer=item.answer,
        explanation=item.explanation,
        visual=hidden(item),
        answer_visual=item.visual,
        node_id=item.node_id,
    )


def to_true_false(item: BlankItem, rng: random.Random) -> IllustratedQuestion:
    correct = rng.random() < 0.5
    shown = item.answer if correct else rng.choice(item.distractors)
    explanation = (
        f"Yes - “{item.answer}” belongs there. {item.explanation}"
        if correct
        else f"No - that box should say “{item.answer}”. {item.explanation}"
    )
    return IllustratedQuestion(
        type="true_false",
        question="Look at the diagram. Is the dashed box labelled correctly?",
        answer="True" if correct else "False",
        explanation=explanation,
        visual=hidden(item, fill=shown),
        answer_visual=item.visual,
        node_id=item.node_id,
    )
