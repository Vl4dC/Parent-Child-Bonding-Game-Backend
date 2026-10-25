"""Shared wallet, quest claims, server-priced purchases and persistent equipment."""

from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app import economy
from app.context import api_context
from app.home_api import CoinsOut, me, reporting, store_of
from learning_service.shop import CATALOG, ShopItem, Slot

router = APIRouter(
    prefix="/api", tags=["Economy"], dependencies=[Depends(api_context, scope="function")]
)


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DuelClaim(StrictBody):
    tournament_id: str | None = Field(None, min_length=1, max_length=128)
    match_id: str | None = Field(None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def one_source(self):
        if (self.tournament_id is None) == (self.match_id is None):
            raise ValueError("Supply exactly one tournament_id or match_id")
        return self


class QuestClaim(StrictBody):
    period: str = Field(
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="The period returned by GET /api/me/quests; stale periods return 409",
    )


class QuestOut(BaseModel):
    id: str
    title: str
    kind: Literal["daily", "weekly"]
    period: str
    resets_at: datetime
    progress: int
    target: int
    reward: int
    claimed: bool
    claimable: bool


class QuestsOut(BaseModel):
    timezone: str = "Europe/Berlin"
    items: list[QuestOut]


class InventoryOut(BaseModel):
    owned: list[str]
    equipped: dict[str, str | None]


class PurchaseIn(StrictBody):
    item_id: str = Field(min_length=1, max_length=128)


class PurchaseOut(BaseModel):
    item_id: str
    charged: int
    coins: int
    inventory: InventoryOut


class EquipIn(StrictBody):
    item_id: str | None = Field(..., min_length=1, max_length=128)


class TransactionOut(BaseModel):
    id: int
    delta: int
    reason: str
    ref: str
    created_at: datetime


class WalletOut(BaseModel):
    coins: int
    transactions: list[TransactionOut]
    next_cursor: int | None


DuelClaimBody = Annotated[
    DuelClaim,
    Body(
        openapi_examples={
            "local": {
                "summary": "Collect a same-device duel reward",
                "value": {"tournament_id": "<tournament id>"},
            },
            "match": {
                "summary": "Collect my parent-child match reward",
                "value": {"match_id": "<match id>"},
            },
        }
    ),
]
QuestClaimBody = Annotated[
    QuestClaim,
    Body(
        openapi_examples={
            "claim": {
                "summary": "Use the period returned by the quest list",
                "value": {"period": "2026-10-25"},
            },
        }
    ),
]
PurchaseBody = Annotated[
    PurchaseIn,
    Body(
        openapi_examples={
            "shirt": {
                "summary": "Buy the navy top for its catalog price",
                "value": {"item_id": "top.navy"},
            },
        }
    ),
]
EquipBody = Annotated[
    EquipIn,
    Body(
        openapi_examples={
            "wear": {
                "summary": "Equip an owned top in the top slot",
                "value": {"item_id": "top.navy"},
            },
            "remove": {"summary": "Unequip the selected slot", "value": {"item_id": None}},
        }
    ),
]


@router.get("/me/wallet", response_model=WalletOut)
def wallet(request: Request, after: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
    return store_of(request).wallet(me(), after, limit)


@router.get("/shop/catalog", response_model=list[ShopItem])
def catalog():
    return CATALOG


@router.get("/me/inventory", response_model=InventoryOut)
def inventory(request: Request):
    return store_of(request).inventory(me())


@router.post(
    "/me/shop/purchases",
    response_model=PurchaseOut,
    responses={402: {"description": "Insufficient coins"}, 404: {"description": "Unknown item"}},
)
def purchase(body: PurchaseBody, request: Request):
    with reporting():
        return store_of(request).purchase(me(), body.item_id)


@router.put("/me/equipment/{slot}", response_model=InventoryOut)
def equip(slot: Slot, body: EquipBody, request: Request):
    with reporting():
        return store_of(request).equip(me(), slot, body.item_id)


@router.get("/me/quests", response_model=QuestsOut)
def quests(request: Request):
    at = economy.clock()
    counts = economy.counts(at)
    claimed = store_of(request).claimed_quests(me())
    items = []
    for id, title, kind, event, target, reward in economy.QUESTS:
        period, _, resets_at = economy.period(kind, at)
        progress = min(target, counts.get((kind, event), 0))
        paid = f"{id}/{period}" in claimed
        items.append(
            QuestOut(
                id=id,
                title=title,
                kind=kind,
                period=period,
                resets_at=resets_at,
                progress=progress,
                target=target,
                reward=reward,
                claimed=paid,
                claimable=progress == target and not paid,
            )
        )
    return QuestsOut(items=items)


@router.post("/me/quests/{quest_id}/claim", response_model=CoinsOut)
def claim_quest(quest_id: str, body: QuestClaimBody, request: Request):
    definition = next((q for q in economy.QUESTS if q[0] == quest_id), None)
    if definition is None:
        raise HTTPException(404, "Quest not found.")
    id, _, kind, event, target, reward = definition
    at = economy.clock()
    period, _, _ = economy.period(kind, at)
    if body.period != period:
        raise HTTPException(409, "Quest period changed. Refresh the quest list.")
    if economy.counts(at).get((kind, event), 0) < target:
        raise HTTPException(409, "Finish the quest before claiming its reward.")
    coins, credited = store_of(request).credit(me(), "quest", [(f"{id}/{period}", reward)])
    return CoinsOut(coins=coins, credited=credited)


@router.post("/me/coins/claim-duel", response_model=CoinsOut)
def claim_duel(body: DuelClaimBody, request: Request):
    if body.tournament_id is not None:
        from learning_service.store import TOURNAMENTS, get_or_404

        quiz = get_or_404(TOURNAMENTS, body.tournament_id, "Tournament")
        duel = (quiz.submission or {}).get("duel")
        if duel is None:
            raise HTTPException(409, "Submit both duel answer sheets first.")
        ref = f"tournament/{quiz.id}"
        amount = economy.duel_reward(duel["result"]["players"][0]["score"])
    else:
        from learning_service import matches

        doc = request.app.state.memory.get_match(me(), body.match_id)
        if doc is None:
            raise HTTPException(404, "Match not found, expired, or unavailable to this player.")
        if doc["mode"] != "parent_child" or doc["game"] != "mcq":
            raise HTTPException(409, "Only parent-child MCQ matches earn duel coins.")
        try:
            rankings = matches.mcq_results(doc, matches.clock())
        except matches.MatchError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        # Both players must actually participate; simply letting a timer expire earns nothing.
        if any(not doc["answers"].get(p["player_id"]) for p in doc["players"]):
            raise HTTPException(409, "Both players must answer at least one question.")
        correct = next(row["correct"] for row in rankings if row["id"] == me())
        amount = economy.duel_reward(correct)
        ref = f"match/{doc['id']}"
        ended = matches.mcq_end(doc, len(doc["questions"]) - 1) + matches.GRACE_MS + 1
        economy.record("duel", ref, datetime.fromtimestamp(ended / 1000, timezone.utc))
    coins, credited = store_of(request).credit_rewards(
        me(),
        [
            ("duel", ref, amount - economy.PARTICIPATION_COINS),
            ("participation", ref, economy.PARTICIPATION_COINS),
        ],
    )
    return CoinsOut(coins=coins, credited=credited)


class ParticipationClaim(StrictBody):
    source: Literal["quiz", "tournament", "match", "diagram_race"]
    id: str = Field(min_length=1, max_length=128)


ParticipationBody = Annotated[
    ParticipationClaim,
    Body(
        openapi_examples={
            "quiz": {
                "summary": "Collect 20 coins after answering the whole quiz",
                "value": {"source": "quiz", "id": "<quiz id>"},
            },
            "match": {
                "summary": "Collect participation coins after a match ends",
                "value": {"source": "match", "id": "<match id>"},
            },
            "race": {
                "summary": "Collect coins after participating in a diagram race",
                "value": {"source": "diagram_race", "id": "<race id>"},
            },
        }
    ),
]


@router.post(
    "/me/coins/claim-participation",
    response_model=CoinsOut,
    responses={
        404: {"description": "Game unavailable to this player"},
        409: {"description": "Game incomplete or player did not participate"},
    },
)
def claim_participation(body: ParticipationBody, request: Request):
    """20 coins once per completed game and participant, regardless of score. No duplicate duel bonus."""
    eligible = False
    if body.source == "quiz":
        from app.quiz_practice import quiz_for

        quiz = quiz_for(body.id)
        eligible = bool(quiz.questions) and len(quiz.attempts) == len(quiz.questions)
    elif body.source == "tournament":
        from learning_service.store import TOURNAMENTS, get_or_404

        quiz = get_or_404(TOURNAMENTS, body.id, "Tournament")
        eligible = bool((quiz.submission or {}).get("result"))
    elif body.source == "match":
        from learning_service import matches

        doc = request.app.state.memory.get_match(me(), body.id)
        if doc is None:
            raise HTTPException(404, "Match not found, expired, or unavailable to this player.")
        try:
            matches.results(doc, matches.clock())
        except matches.MatchError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        if doc["game"] == "mcq":
            eligible = bool(doc["answers"].get(me()))
            if doc["mode"] == "parent_child" and all(
                doc["answers"].get(p["player_id"]) for p in doc["players"]
            ):
                ended = matches.mcq_end(doc, len(doc["questions"]) - 1) + matches.GRACE_MS + 1
                economy.record(
                    "duel", f"match/{body.id}", datetime.fromtimestamp(ended / 1000, timezone.utc)
                )
        elif doc["game"] == "heads_up":
            involved = {t["index"] for t in doc["turns"] if me() in (t["guesser"], t["explainer"])}
            eligible = any(event["turn"] in involved for event in doc["log"])
        else:
            mine = doc["progress"][me()]
            eligible = mine["finished_ms"] is not None or mine["wrong_finishes"] > 0
    else:
        from app.diagram_game import DiagramGame

        if not request.app.state.settings.diagram_assembly_enabled:
            raise HTTPException(503, "Diagram assembly is not enabled.")
        game = DiagramGame(
            request.app.state.repository, getattr(request.app.state, "diagram_clock_ms", None)
        )
        eligible = game.participated(body.id, me())
    if not eligible:
        raise HTTPException(409, "Complete the game and participate before claiming coins.")
    coins, credited = store_of(request).credit(
        me(),
        "participation",
        [
            (f"{body.source}/{body.id}", economy.PARTICIPATION_COINS),
        ],
    )
    return CoinsOut(coins=coins, credited=credited)
