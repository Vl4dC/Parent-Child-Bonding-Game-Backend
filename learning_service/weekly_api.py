"""
The weekly challenge preview over HTTP. SHOW ONLY: see weekly.py.

  GET  /api/weekly/next?band=      next week's challenge: topic, games, modes, rules and any prepared samples
  GET  /api/weekly/current?band=   this week's, the same way
  POST /api/weekly/next/prepare    have the AI write next week's samples (cached for the week)
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.generation import generation_permit
from learning_service import weekly
from learning_service.api_docs import WEEKLY, errors
from learning_service.challenges import ChallengeFailed
from learning_service.memory_store import MemoryStore
from learning_service.safety import AgeBlocked, SafetyUnavailable
from learning_service.weekly import Band

log = logging.getLogger("weekly_api")
router = APIRouter(prefix="/api/weekly", tags=[WEEKLY])


class WeeklyPreview(BaseModel):
    preview: bool = Field(True, description="Always true for now: weekly play is not open")
    notice: str
    label: str
    which: str
    week: dict[str, str]
    band: str
    age: int = Field(..., description="The age the band's content is written for")
    topic: str
    subject: str
    blurb: str
    games: list[dict[str, str]]
    modes: list[dict[str, str]]
    rules: list[str]
    samples_ready: dict[str, bool]
    samples: dict[str, dict[str, Any] | None] = Field(
        ..., description="A taste of each game, without answers; null until prepared"
    )
    example_leaderboard: list[dict[str, Any]] = Field(..., description="Made up, to show the idea")


class PrepareIn(BaseModel):
    band: Band = "10-12"
    games: list[Literal["mcq", "heads_up", "diagram"]] | None = Field(
        None, description="Which games to prepare; leave out for all three"
    )


def memory_of(request: Request) -> MemoryStore:
    return request.app.state.memory


@router.get("/next", summary="Next week's challenge (preview)")
def next_week(request: Request, band: Band = "10-12") -> WeeklyPreview:
    """What next week's tournament will be: a common school topic, the three games, the four modes, the rules, a few
    sample questions, cards and a diagram (once prepared), and an example leaderboard. Preview only: nothing can be
    played or scored yet."""
    return WeeklyPreview(**weekly.preview(memory_of(request), "next", band))


@router.get("/current", summary="This week's challenge (preview)")
def current_week(request: Request, band: Band = "10-12") -> WeeklyPreview:
    """The same preview for the current week."""
    return WeeklyPreview(**weekly.preview(memory_of(request), "current", band))


@router.post(
    "/next/prepare",
    summary="Prepare next week's samples",
    responses=errors(422, 502),
    dependencies=[Depends(generation_permit, scope="function")],
)
def prepare_next_week(
    request: Request,
    body: Annotated[
        PrepareIn,
        Body(
            openapi_examples={
                "all": {
                    "summary": "All three games for the youngest league",
                    "value": {"band": "10-12"},
                },
                "one": {"summary": "Only the quiz", "value": {"band": "13-15", "games": ["mcq"]}},
            }
        ),
    ],
) -> WeeklyPreview:
    """Have the AI write the sample content for next week's topic, checked for the band's age and cached for the week,
    so showing the preview again costs nothing. Games already prepared are kept. Takes about 10-60 s the first time.
    Returns the preview."""
    store = memory_of(request)
    try:
        weekly.prepare(store, "next", body.band, body.games)
    except (AgeBlocked, SafetyUnavailable):
        raise
    except ChallengeFailed as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        log.warning("weekly sample generation failed")
        raise HTTPException(502, "The samples couldn't be written. Try again later.") from exc
    return WeeklyPreview(**weekly.preview(store, "next", body.band))
