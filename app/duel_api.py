"""Typed match lifecycle, private attempts and finish-gated duel results."""

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.duel_game import DuelGame
from app.generation import reserve_request
from app.player import player_id
from learning_service.store import PublicQuestion

router = APIRouter(
    prefix="/api/duels",
    tags=["Duel matches"],
    responses={
        403: {"description": "Host or source-sharing role rejected"},
        404: {"description": "Source/match/participation absent or unavailable to this actor"},
        409: {"description": "Stale version, source/phase/readiness conflict or conflicting retry"},
        422: {"description": "Invalid input or unsupported reviewed snapshot"},
        429: {"description": "Request, retained-history or action limit reached"},
        503: {"description": "Feature, migration, identity keys or storage unavailable"},
    },
)
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]
Id = Annotated[str, Path(min_length=1, max_length=128)]
Kind = Literal["quiz", "tournament"]
Status = Literal["waiting", "countdown", "running", "finished", "expired", "cancelled"]


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateDuel(StrictInput):
    source_kind: Kind
    source_id: str = Field(min_length=1, max_length=128)
    expected_question_set_id: str = Field(min_length=1, max_length=128)
    mode: Literal["local", "room"]
    room_id: str | None = Field(None, min_length=1, max_length=128)
    expected_room_version: int | None = Field(None, ge=1, strict=True)
    players: list[Annotated[str, Field(min_length=1, max_length=60)]] | None = Field(
        None, min_length=2, max_length=2
    )
    question_indices: list[Annotated[int, Field(ge=0, le=49, strict=True)]] | None = Field(
        None, min_length=1, max_length=50
    )

    @model_validator(mode="after")
    def mode_fields(self):
        if self.mode == "room":
            if (
                self.room_id is None
                or self.expected_room_version is None
                or self.players is not None
            ):
                raise ValueError(
                    "Room duels require room ID/version and capture names from membership"
                )
        elif (
            self.players is None
            or self.room_id is not None
            or self.expected_room_version is not None
        ):
            raise ValueError("Local duels require exactly two display names and no room fields")
        if self.players is not None:
            self.players = [p.strip() for p in self.players]
            if (
                any(
                    not p or any(ord(c) < 32 for c in p) or len(p.splitlines()) != 1
                    for p in self.players
                )
                or len({p.casefold() for p in self.players}) != 2
            ):
                raise ValueError("Choose two distinct nonempty single-line player names")
        return self


class Version(StrictInput):
    expected_version: int = Field(ge=1, strict=True)


class Ready(Version):
    ready: bool = Field(strict=True)


class Submit(Version):
    answers: list[Annotated[int, Field(ge=0, le=3, strict=True)]] = Field(
        min_length=1, max_length=50
    )


class Rematch(StrictInput):
    expected_room_version: int | None = Field(None, ge=1, strict=True)


class SourceInfo(BaseModel):
    id: str
    source_kind: Kind
    question_set_id: str
    title: str
    total: int
    approved: bool
    lesson_approved: bool


class Availability(BaseModel):
    available: bool
    reason: Literal["source_unavailable", "source_unapproved", "source_changed"] | None


class Player(BaseModel):
    id: str
    display_name: str
    version: int
    ready: bool
    submitted: bool
    forfeited: bool


class Match(BaseModel):
    id: str
    mode: Literal["local", "room"]
    room_id: str | None
    title: str
    status: Status
    version: int
    total: int
    created_at: int
    started_at: int | None
    expires_at: int
    server_now: int
    my_player_id: str | None
    players: list[Player]
    source: Availability
    reason: str | None


class MatchHistory(BaseModel):
    items: list[Match]
    next_cursor: str | None


class Management(BaseModel):
    room_id: str
    match_id: str
    status: Status
    version: int
    reason: str | None


class Action(BaseModel):
    id: str
    status: Status
    version: int
    reason: str | None
    started_at: int | None
    expires_at: int


class PlayerAction(BaseModel):
    id: str
    player_id: str
    version: int
    match_version: int
    status: Status
    ready: bool
    submitted: bool
    forfeited: bool


class PlayerAttempt(BaseModel):
    id: str
    player_id: str
    version: int
    answers: list[int] | None
    ready: bool
    submitted: bool
    forfeited: bool
    status: Status


class Questions(BaseModel):
    id: str
    title: str
    questions: list[PublicQuestion]
    started_at: int
    expires_at: int
    server_now: int


class PlayerResult(BaseModel):
    id: str
    display_name: str
    submitted: bool
    forfeited: bool
    score: int | None
    total: int
    rank: int | None


class Results(BaseModel):
    id: str
    status: Literal["finished", "expired"]
    players: list[PlayerResult]
    winner_ids: list[str]
    scoring: Literal["correct_answers_only"]
    source: Availability


class AnswerResult(BaseModel):
    index: int
    choice: int
    correct: bool
    answer_index: int
    explanation: str
    answer_visual_html: str | None


class ReviewedAttempt(BaseModel):
    player_id: str
    answers: list[int] | None
    results: list[AnswerResult] | None


class Solution(BaseModel):
    index: int
    answer_index: int
    explanation: str
    answer_visual_html: str | None


class Review(Results):
    questions: list[PublicQuestion]
    attempts: list[ReviewedAttempt]
    solution: list[Solution]


async def actor(request: Request, owner: Annotated[str, Depends(player_id)]):
    if not request.app.state.settings.duels_enabled:
        raise HTTPException(503, "Duel matches are not enabled.")
    repo = request.app.state.repository
    if repo is None:
        raise HTTPException(503, "Application storage is unavailable.")
    await asyncio.to_thread(
        reserve_request, repo, owner, request.app.state.settings.api_requests_per_minute
    )
    from app.practice_readiness import check

    if not (await asyncio.to_thread(check, repo, request.app.state.settings, "duels"))["ready"]:
        raise HTTPException(503, "Duel storage is not ready; apply and verify its migration.")
    return owner, DuelGame(repo, getattr(request.app.state, "duel_clock_ms", None))


Actor = Annotated[tuple, Depends(actor)]


@router.get("/sources/{source_kind}/{source_id}", response_model=SourceInfo)
async def source(source_kind: Kind, source_id: Id, identity: Actor):
    owner, game = identity
    return await asyncio.to_thread(
        game.source_info, owner, "quizzes" if source_kind == "quiz" else "tournaments", source_id
    )


@router.post("", status_code=201, response_model=Match)
async def create(body: CreateDuel, key: Key, identity: Actor):
    owner, game = identity
    return await asyncio.to_thread(game.create, owner, body.model_dump(), key)


@router.get("/history", response_model=MatchHistory)
async def history(
    identity: Actor, after: str = Query("", max_length=128), limit: int = Query(20, ge=1, le=100)
):
    owner, game = identity
    return await asyncio.to_thread(game.history, owner, after, limit)


@router.get("/rooms/{room_id}/match", response_model=Match)
async def current(room_id: Id, identity: Actor):
    owner, game = identity
    return await asyncio.to_thread(game.current, room_id, owner)


@router.get("/rooms/{room_id}/management", response_model=Management)
async def management(room_id: Id, identity: Actor):
    owner, game = identity
    return await asyncio.to_thread(game.current, room_id, owner, True)


@router.get("/{match_id}", response_model=Match)
async def snapshot(match_id: Id, identity: Actor):
    owner, game = identity
    return await asyncio.to_thread(game.snapshot, match_id, owner)


@router.get("/{match_id}/questions", response_model=Questions)
async def questions(match_id: Id, identity: Actor):
    owner, game = identity
    return await asyncio.to_thread(game.questions, match_id, owner)


@router.get("/{match_id}/players/{player_id}/attempt", response_model=PlayerAttempt)
async def attempt(match_id: Id, player_id: Id, identity: Actor):
    owner, game = identity
    return await asyncio.to_thread(game.player_state, match_id, player_id, owner)


@router.put("/{match_id}/players/{player_id}/ready", response_model=PlayerAction)
async def ready(match_id: Id, player_id: Id, body: Ready, key: Key, identity: Actor):
    owner, game = identity
    return await asyncio.to_thread(
        game.player_action, match_id, player_id, owner, body.model_dump(), key, "ready"
    )


@router.post("/{match_id}/players/{player_id}/submit", response_model=PlayerAction)
async def submit(match_id: Id, player_id: Id, body: Submit, key: Key, identity: Actor):
    owner, game = identity
    return await asyncio.to_thread(
        game.player_action, match_id, player_id, owner, body.model_dump(), key, "submit"
    )


@router.post("/{match_id}/players/{player_id}/forfeit", response_model=PlayerAction)
async def forfeit(match_id: Id, player_id: Id, body: Version, key: Key, identity: Actor):
    owner, game = identity
    return await asyncio.to_thread(
        game.player_action, match_id, player_id, owner, body.model_dump(), key, "forfeit"
    )


@router.post("/{match_id}/start", response_model=Action)
async def start(match_id: Id, body: Version, key: Key, identity: Actor):
    owner, game = identity
    return await asyncio.to_thread(
        game.start_or_cancel, match_id, owner, body.model_dump(), key, "start"
    )


@router.post("/{match_id}/cancel", response_model=Action)
async def cancel(match_id: Id, body: Version, key: Key, identity: Actor):
    owner, game = identity
    return await asyncio.to_thread(
        game.start_or_cancel, match_id, owner, body.model_dump(), key, "cancel"
    )


@router.get("/{match_id}/results", response_model=Results)
async def results(match_id: Id, identity: Actor):
    owner, game = identity
    return await asyncio.to_thread(game.results, match_id, owner)


@router.get("/{match_id}/review", response_model=Review)
async def review(match_id: Id, identity: Actor):
    owner, game = identity
    return await asyncio.to_thread(game.results, match_id, owner, True)


@router.post("/{match_id}/rematch", status_code=201, response_model=Match)
async def rematch(match_id: Id, body: Rematch, key: Key, identity: Actor):
    owner, game = identity
    return await asyncio.to_thread(game.rematch, match_id, owner, body.model_dump(), key)
