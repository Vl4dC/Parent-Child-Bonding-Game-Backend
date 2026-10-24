"""Data the visualization harness passes around."""
from __future__ import annotations

from pydantic import BaseModel, Field

from lesson import MAX_VISUALS, Lesson, SectionVisual, VisualKind

__all__ = ["Visual", "VisualDraft", "VisualKind", "VisualPlan", "VisualPlanItem", "VisualRequest"]


class VisualRequest(BaseModel):
    """What the harness is asked to illustrate."""

    topic: str
    age: int = Field(..., ge=5, le=99)
    lesson: Lesson
    ask: str | None = Field(None, description="Learner's own request, e.g. 'show me the light reactions'. "
                                              "None = draw the visuals the lesson planned")
    max_visuals: int = Field(MAX_VISUALS, ge=1, le=MAX_VISUALS)
    note: str = Field("", description="On a rewrite: what a reviewer found unsuitable for the age (safety.py)")


class VisualPlanItem(SectionVisual):
    section_index: int = Field(..., description="0-based index of the lesson section this visual explains")


class VisualPlan(BaseModel):
    items: list[VisualPlanItem] = Field(default_factory=list, description="Empty when no visual would help")


class VisualDraft(BaseModel):
    """What the drawer model writes for one plan item."""

    title: str = Field(..., description="Short heading shown above the visual")
    caption: str = Field(..., description="One sentence: what to notice")
    alt: str = Field(..., description="Text description for screen readers")
    code: str = Field(..., description="The visual's source, in the format the skill specifies")


class Visual(VisualDraft):
    kind: VisualKind
    section_index: int
