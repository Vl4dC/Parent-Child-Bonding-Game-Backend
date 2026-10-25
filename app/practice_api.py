"""Typed, owner-scoped deterministic flashcard attempts and restarts."""

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.card_practice import CardPractice
from app.generation import reserve_request
from app.player import player_id
from learning_service.flashcards import DeckState, PublicCard

router = APIRouter(
    tags=["Flashcard attempts"],
    responses={
        404: {"description": "Saved deck/attempt absent or unavailable to this actor"},
        409: {"description": "Stale version, inactive attempt, empty subset or conflicting retry"},
        422: {"description": "Invalid input or oversized saved snapshot"},
        429: {"description": "Request, retained-history or action limit reached"},
        503: {"description": "Feature, migration, identity keys or storage unavailable"},
    },
)
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]
Id = Annotated[str, Path(min_length=1, max_length=128)]


class CreateAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    which: Literal["all", "hard"] = "all"


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)
    card: int = Field(ge=0, le=99, strict=True)
    result: Literal["know", "again"]


class AttemptSummary(BaseModel):
    id: str
    source_deck_id: str
    lesson_id: str
    title: str
    source_hash: str
    parent_id: str | None
    status: Literal["active", "completed", "abandoned"]
    version: int
    created_at: int
    completed_at: int | None
    abandoned_at: int | None
    state: DeckState


class Attempt(AttemptSummary):
    cards: list[PublicCard]


class AttemptHistory(BaseModel):
    items: list[AttemptSummary]
    next_cursor: str | None


async def actor(request: Request, owner: Annotated[str, Depends(player_id)]):
    if not request.app.state.settings.flashcard_attempts_enabled:
        raise HTTPException(503, "Flashcard attempts are not enabled.")
    repo = request.app.state.repository
    if repo is None:
        raise HTTPException(503, "Application storage is unavailable.")
    await asyncio.to_thread(
        reserve_request, repo, owner, request.app.state.settings.api_requests_per_minute
    )
    from app.practice_readiness import check

    if not (await asyncio.to_thread(check, repo, request.app.state.settings, "flashcard_attempts"))[
        "ready"
    ]:
        raise HTTPException(
            503, "Flashcard attempt storage is not ready; apply and verify its migration."
        )
    return owner, CardPractice(repo)


Actor = Annotated[tuple, Depends(actor)]


@router.post("/api/flashcards/{deck_id}/attempts", status_code=201, response_model=Attempt)
async def create(deck_id: Id, body: CreateAttempt, key: Key, identity: Actor):
    owner, service = identity
    return await asyncio.to_thread(service.create, owner, deck_id, body.which, key)


@router.get("/api/flashcards/{deck_id}/attempts", response_model=AttemptHistory)
async def history(
    deck_id: Id,
    identity: Actor,
    after: str = Query("", max_length=128),
    limit: int = Query(20, ge=1, le=100),
):
    owner, service = identity
    return await asyncio.to_thread(service.history, owner, deck_id, after, limit)


@router.get("/api/flashcard-attempts/{attempt_id}", response_model=Attempt)
async def get(attempt_id: Id, identity: Actor):
    owner, service = identity
    return await asyncio.to_thread(service.get, owner, attempt_id)


@router.post("/api/flashcard-attempts/{attempt_id}/reviews", response_model=AttemptSummary)
async def review_attempt(attempt_id: Id, body: ReviewInput, key: Key, identity: Actor):
    owner, service = identity
    return await asyncio.to_thread(service.review, owner, attempt_id, body.model_dump(), key)


@router.post(
    "/api/flashcard-attempts/{attempt_id}/restart", status_code=201, response_model=Attempt
)
async def restart(attempt_id: Id, body: CreateAttempt, key: Key, identity: Actor):
    owner, service = identity
    return await asyncio.to_thread(service.create, owner, None, body.which, key, attempt_id)


@router.get("/api/flashcard-attempts/{attempt_id}/results", response_model=AttemptSummary)
async def results(attempt_id: Id, identity: Actor):
    owner, service = identity
    return await asyncio.to_thread(service.get, owner, attempt_id, True)
