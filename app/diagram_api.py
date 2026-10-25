"""Typed API for frozen visual snapshots and simultaneous label-assembly races."""

import asyncio
import threading
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from app.diagram_game import DiagramGame
from app.diagram_render import render_snapshot
from app.generation import reserve_request
from app.player import player_id

router = APIRouter(
    prefix="/api/diagram-assembly",
    tags=["Diagram assembly"],
    responses={
        404: {"description": "Source/race absent or access denied"},
        409: {"description": "Stale version, not ready, or incompatible race phase"},
        422: {"description": "Unsupported diagram or invalid placement"},
        429: {"description": "Request limit or renderer busy"},
        503: {"description": "Feature, renderer or storage unavailable"},
    },
)
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]
ItemId = Annotated[str, Path(min_length=1, max_length=128)]


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TemplateInput(StrictInput):
    lesson_id: str = Field(min_length=1, max_length=128)
    visual_id: str = Field(min_length=1, max_length=128)


class RaceInput(StrictInput):
    template_id: str = Field(min_length=1, max_length=128)
    room_id: str = Field(min_length=1, max_length=128)
    expected_room_version: int = Field(ge=1, strict=True)


class VersionInput(StrictInput):
    expected_version: int = Field(ge=1, strict=True)


class PlacementsInput(VersionInput):
    placements: dict[
        Annotated[str, Field(min_length=1, max_length=64)],
        Annotated[str, Field(min_length=1, max_length=64)],
    ] = Field(max_length=40)


class TemplateInfo(BaseModel):
    id: str
    lesson_id: str
    visual_id: str
    source_hash: str
    created_at: int
    slots: int
    renderer: dict
    snapshot_version: Literal[1]


class RaceCreated(BaseModel):
    id: str
    room_id: str
    version: int
    status: Literal["waiting", "running", "finished", "expired", "cancelled"]


class RaceAction(BaseModel):
    id: str
    version: int
    status: Literal["running", "countdown", "cancelled"]
    started_at: int | None
    deadline_at: int | None
    reason: str | None = None


class SourceAvailability(BaseModel):
    available: bool
    reason: Literal["source_unavailable", "source_unapproved", "source_changed"] | None


class RaceManagement(BaseModel):
    room_id: str
    race_id: str
    version: int
    status: Literal["waiting", "countdown", "running", "finished", "expired", "cancelled"]
    reason: str | None = None


class RaceMember(BaseModel):
    id: str
    display_name: str
    placed: int
    completed: bool
    forfeited: bool


class RaceSnapshot(BaseModel):
    id: str
    room_id: str
    version: int
    status: Literal["waiting", "countdown", "running", "finished", "expired", "cancelled"]
    question: Literal["How does this work?"]
    total: int
    started_at: int | None
    deadline_at: int | None
    server_now: int
    my_player_id: str
    players: list[RaceMember]
    reason: str | None = None
    source: SourceAvailability


class Slot(BaseModel):
    id: str
    x: float
    y: float
    width: float
    height: float


class Card(BaseModel):
    id: str
    text: str


class Board(BaseModel):
    id: str
    question: Literal["How does this work?"]
    puzzle_svg: str
    view_box: str
    slots: list[Slot]
    cards: list[Card]
    placements: dict[str, str]
    version: int
    started_at: int
    deadline_at: int
    server_now: int


class PlayerAction(BaseModel):
    id: str
    version: int
    placed: int
    total: int
    completed: bool
    elapsed_ms: int | None
    submissions: int
    correct_count: int | None
    incorrect_slot_ids: list[str] | None
    forfeited: bool


class Study(BaseModel):
    id: str
    question: Literal["How does this work?"]
    reference_svg: str
    view_box: str


class PlayerResult(BaseModel):
    id: str
    display_name: str
    completed: bool
    elapsed_ms: int | None
    rank: int | None
    forfeited: bool
    submissions: int


class Results(BaseModel):
    id: str
    status: Literal["finished", "expired"]
    players: list[PlayerResult]
    winner_ids: list[str]
    scoring: Literal["fastest_correct_server_elapsed"]
    source: SourceAvailability


class Solution(BaseModel):
    slot_id: str
    text: str


class Review(Results):
    reference_svg: str
    solution: list[Solution]


@dataclass
class Actor:
    owner: str
    game: DiagramGame
    request: Request


async def actor(request: Request, owner: Annotated[str, Depends(player_id)]):
    settings = request.app.state.settings
    if not settings.diagram_assembly_enabled:
        raise HTTPException(503, "Diagram assembly is not enabled.")
    repository = request.app.state.repository
    if repository is None:
        raise HTTPException(503, "Application storage is unavailable.")
    await asyncio.to_thread(reserve_request, repository, owner, settings.requests_per_minute)
    return Actor(
        owner,
        DiagramGame(repository, getattr(request.app.state, "diagram_clock_ms", None)),
        request,
    )


Identity = Annotated[Actor, Depends(actor)]


@router.post("/templates", status_code=201, response_model=TemplateInfo)
async def create_template(body: TemplateInput, key: Key, identity: Identity):
    prepared = await asyncio.to_thread(
        identity.game.prepare_template, identity.owner, body.model_dump(), key
    )
    if "ready" in prepared:
        return prepared["ready"]
    snapshot = prepared["cached"]["snapshot"] if prepared["cached"] else None
    if snapshot is None:
        slots = identity.request.app.state.diagram_render_slots
        if not slots.acquire(blocking=False):
            raise HTTPException(429, "Diagram rendering is busy; retry with the same key.")
        try:
            snapshot = await asyncio.to_thread(
                identity.request.app.state.diagram_renderer,
                prepared["visual"]["code"],
                identity.request.app.state.settings,
            )
        finally:
            slots.release()
    return await asyncio.to_thread(
        identity.game.commit_template, identity.owner, body.model_dump(), key, prepared, snapshot
    )


@router.get("/templates/{template_id}", response_model=TemplateInfo)
async def template(template_id: ItemId, identity: Identity):
    return await asyncio.to_thread(identity.game.template, identity.owner, template_id)


@router.post("/races", status_code=201, response_model=RaceCreated)
async def create_race(body: RaceInput, key: Key, identity: Identity):
    return await asyncio.to_thread(
        identity.game.create_race, identity.owner, body.model_dump(), key
    )


@router.get("/races/{race_id}", response_model=RaceSnapshot)
async def race(race_id: ItemId, identity: Identity):
    return await asyncio.to_thread(identity.game.snapshot, race_id, identity.owner)


@router.get("/rooms/{room_id}/race", response_model=RaceSnapshot)
async def current_race(room_id: ItemId, identity: Identity):
    return await asyncio.to_thread(identity.game.current, room_id, identity.owner)


@router.post("/races/{race_id}/start", response_model=RaceAction)
async def start(race_id: ItemId, body: VersionInput, key: Key, identity: Identity):
    return await asyncio.to_thread(
        identity.game.start, race_id, identity.owner, body.model_dump(), key
    )


@router.get("/rooms/{room_id}/management", response_model=RaceManagement)
async def management(room_id: ItemId, identity: Identity):
    return await asyncio.to_thread(identity.game.management, room_id, identity.owner)


@router.get("/races/{race_id}/board", response_model=Board)
async def board(race_id: ItemId, identity: Identity):
    return await asyncio.to_thread(identity.game.board, race_id, identity.owner)


@router.get("/races/{race_id}/study", response_model=Study)
async def study(race_id: ItemId, identity: Identity):
    return await asyncio.to_thread(identity.game.study, race_id, identity.owner)


@router.put("/races/{race_id}/placements", response_model=PlayerAction)
async def placements(race_id: ItemId, body: PlacementsInput, key: Key, identity: Identity):
    return await asyncio.to_thread(
        identity.game.update_player, race_id, identity.owner, "placements", body.model_dump(), key
    )


@router.post("/races/{race_id}/finish", response_model=PlayerAction)
async def finish(race_id: ItemId, body: VersionInput, key: Key, identity: Identity):
    return await asyncio.to_thread(
        identity.game.update_player, race_id, identity.owner, "finish", body.model_dump(), key
    )


@router.post("/races/{race_id}/forfeit", response_model=PlayerAction)
async def forfeit(race_id: ItemId, body: VersionInput, key: Key, identity: Identity):
    return await asyncio.to_thread(
        identity.game.update_player, race_id, identity.owner, "forfeit", body.model_dump(), key
    )


@router.get("/races/{race_id}/results", response_model=Results)
async def results(race_id: ItemId, identity: Identity):
    return await asyncio.to_thread(identity.game.results, race_id, identity.owner)


@router.get("/races/{race_id}/review", response_model=Review)
async def review(race_id: ItemId, identity: Identity):
    return await asyncio.to_thread(identity.game.results, race_id, identity.owner, True)


@router.post("/races/{race_id}/cancel", response_model=RaceAction)
async def cancel(race_id: ItemId, body: VersionInput, key: Key, identity: Identity):
    return await asyncio.to_thread(
        identity.game.cancel, race_id, identity.owner, body.model_dump(), key
    )


def install(app):
    from app.diagram_preflight import DiagramPreflight

    app.state.diagram_render_slots = threading.BoundedSemaphore(
        app.state.settings.diagram_render_concurrency
    )
    app.state.diagram_renderer = render_snapshot
    app.state.diagram_preflight = DiagramPreflight(render_slots=app.state.diagram_render_slots)
    app.include_router(router)
