"""Data the visualization harness passes around."""

from __future__ import annotations

from pydantic import BaseModel, Field

from learning_service.lesson import MAX_VISUALS, Lesson, SectionVisual, VisualKind

__all__ = [
    "DiagramReview",
    "Visual",
    "VisualDraft",
    "VisualKind",
    "VisualPlan",
    "VisualPlanItem",
    "VisualRequest",
]


class VisualRequest(BaseModel):
    """What the harness is asked to illustrate."""

    topic: str
    age: int = Field(..., ge=5, le=99)
    lesson: Lesson
    ask: str | None = Field(
        None,
        description="Learner's own request, e.g. 'show me the light reactions'. "
        "None = draw the visuals the lesson planned",
    )
    max_visuals: int = Field(MAX_VISUALS, ge=1, le=MAX_VISUALS)


class VisualPlanItem(SectionVisual):
    section_index: int = Field(
        ..., description="0-based index of the lesson section this visual explains"
    )


class VisualPlan(BaseModel):
    items: list[VisualPlanItem] = Field(
        default_factory=list, description="Empty when no visual would help"
    )


class VisualDraft(BaseModel):
    """What the drawer model writes for one plan item."""

    title: str = Field(..., description="Short heading shown above the visual")
    caption: str = Field(..., description="One sentence: what to notice")
    alt: str = Field(..., description="Text description for screen readers")
    code: str = Field(..., description="The visual's source, in the format the skill specifies")


class DiagramReview(BaseModel):
    """A second model's verdict on a drawn diagram."""

    ok: bool = Field(..., description="True only if the diagram passes every check")
    problems: list[str] = Field(
        default_factory=list,
        description="When not ok: 1-3 short instructions naming the boxes to change or add",
    )


class Visual(VisualDraft):
    kind: VisualKind
    section_index: int
