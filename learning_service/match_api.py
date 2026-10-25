"""
Playing a challenge over HTTP. The server runs the clock (matches.py); clients send actions and poll the state.

  POST /api/matches                       make a match from one of your challenges (solo, friends, parent_child, duo)
  POST /api/matches/{id}/start            the creator starts the shared countdown
  GET  /api/matches/{id}                  what this player should see right now (poll about once a second)
  POST /api/matches/{id}/answer           mcq: answer the open question
  POST /api/matches/{id}/cards            heads_up: mark the current card
  PUT  /api/matches/{id}/placements       diagram: where the words go
  POST /api/matches/{id}/finish           diagram: check the arrangement
  GET  /api/matches/{id}/results          the ranking, once the game is over
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.repository import current_session
from learning_service import matches
from learning_service.api_docs import MATCHES, MatchId, errors
from learning_service.matches import MatchError, Mode
from learning_service.memory_store import MemoryStore

router = APIRouter(prefix="/api/matches", tags=[MATCHES])


class CreateMatch(BaseModel):
    challenge_id: str = Field(..., description="One of your challenges (`POST /api/challenges`)")
    mode: Mode = Field(
        ...,
        description="`solo`: just you. `friends`: everyone in the challenge's room, each for themselves. "
        "`parent_child`: exactly one parent and one learner; the parent gets two thirds of the time. "
        "`duo`: four players in two teams of two (see `teams`); parents get two thirds of the time.",
    )
    teams: list[list[str]] | None = Field(
        None, description="Duo only: two lists of two player ids, for example [[a, b], [c, d]]"
    )


class AnswerIn(BaseModel):
    question_index: int = Field(
        ..., ge=0, le=30, description="The question that is open now, from 0"
    )
    choice: int = Field(..., ge=0, le=3, description="The option you chose, from 0")


class CardIn(BaseModel):
    action: Literal["correct", "skip", "violation"] = Field(
        ...,
        description="`correct` +1, `skip` 0, `violation` -1 (the explainer said a forbidden word)",
    )
    card_index: int = Field(..., ge=0, le=200, description="The card shown now (its `index`)")


class PlacementsIn(BaseModel):
    placements: dict[str, int] = Field(
        ...,
        description="Your whole arrangement: slot id to the index of the word in `options`. Sent again, it replaces.",
    )


class PlayerOut(BaseModel):
    player_id: str
    name: str
    role: str | None
    team: int | None


class MatchState(BaseModel):
    can_start: bool = False
    id: str
    challenge_id: str
    game: Literal["mcq", "heads_up", "diagram"]
    mode: Mode
    topic: str
    phase: str = Field(
        ..., description="waiting, countdown, then game-specific phases, finally finished"
    )
    server_time_ms: int = Field(
        ..., description="The server's clock: use it to line your timer up, never send times back"
    )
    play_at_ms: int | None = Field(None, description="When play begins (set by `start`)")
    you: str
    players: list[PlayerOut]
    view: dict[str, Any] = Field(
        ...,
        description="What this player sees. Its shape depends on `game`: see the Matches section of API_PLATFORM.md",
    )


class Results(BaseModel):
    id: str
    game: str
    mode: Mode
    topic: str
    ranking: list[dict[str, Any]] = Field(
        ..., description="Best first; equal results share a rank; `winner` marks rank 1"
    )


def memory_of(request: Request) -> MemoryStore:
    return request.app.state.memory


def player_id() -> str:
    return current_session.get().owner


def gone() -> HTTPException:
    return HTTPException(404, "Match not found, expired, or unavailable to this player.")


def play(store: MemoryStore, match_id: str, change) -> dict:
    """Run `change(doc)` on the match atomically, turning the engine's refusals into HTTP errors."""
    try:
        out = store.mutate_match(player_id(), match_id, change)
    except MatchError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    if out is None:
        raise gone()
    return out


def is_game(doc: dict, game: str) -> None:
    if doc["game"] != game:
        raise MatchError(409, f"This match is not a {game} game.")


CreateBody = Annotated[
    CreateMatch,
    Body(
        openapi_examples={
            "solo": {
                "summary": "Play alone",
                "value": {"challenge_id": "<challenge id>", "mode": "solo"},
            },
            "friends": {
                "summary": "Friends in a room",
                "value": {"challenge_id": "<challenge id>", "mode": "friends"},
            },
            "parent_child": {
                "summary": "A parent against a child (the parent gets less time)",
                "value": {"challenge_id": "<challenge id>", "mode": "parent_child"},
            },
            "duo": {
                "summary": "Two teams of two",
                "value": {
                    "challenge_id": "<challenge id>",
                    "mode": "duo",
                    "teams": [["<a>", "<b>"], ["<c>", "<d>"]],
                },
            },
        }
    ),
]
AnswerBody = Annotated[
    AnswerIn,
    Body(
        openapi_examples={
            "first": {
                "summary": "Question 0, first option",
                "value": {"question_index": 0, "choice": 0},
            }
        }
    ),
]
CardBody = Annotated[
    CardIn,
    Body(
        openapi_examples={
            "correct": {"summary": "Got it", "value": {"action": "correct", "card_index": 0}},
            "skip": {"summary": "Skip", "value": {"action": "skip", "card_index": 0}},
        }
    ),
]
PlacementBody = Annotated[
    PlacementsIn,
    Body(
        openapi_examples={
            "two": {"summary": "Two words placed", "value": {"placements": {"n1": 0, "n2": 3}}}
        }
    ),
]


@router.post(
    "", status_code=201, summary="Make a match from a challenge", responses=errors(404, 422)
)
def create_match(body: CreateBody, request: Request) -> MatchState:
    """Turn one of your challenges into a match that the server will run. The players are the challenge's room (or
    just you in `solo`). Checks the mode: `parent_child` needs one parent and one learner with memory profiles, `duo`
    needs four players and `teams`, heads-up needs at least two players, and duo is not available for the diagram
    game yet. Nothing starts until the creator calls `start`."""
    store, creator = memory_of(request), player_id()
    challenge = store.get_challenge(creator, body.challenge_id)
    if challenge is None or challenge.creator_id != creator:
        raise HTTPException(404, "Challenge not found, expired, or not yours.")
    profiles = {pid: store.get_profile(pid) for pid in challenge.players}
    try:
        doc = matches.new_match(challenge, creator, body.mode, body.teams, profiles)
    except MatchError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    store.save_match(
        doc["id"],
        challenge.id,
        creator,
        [p["player_id"] for p in doc["players"]],
        doc,
        matches.expires_at(),
    )
    return MatchState(**matches.state(doc, creator, matches.clock()))


@router.post("/{match_id}/start", summary="Start the countdown", responses=errors(403, 404))
def start_match(match_id: MatchId, request: Request) -> MatchState:
    """The creator starts a shared five-second countdown, after which play begins for everyone at the same instant
    (`play_at_ms`). Starting again changes nothing."""
    store, pid = memory_of(request), player_id()
    play(store, match_id, lambda doc: matches.start(doc, pid, matches.clock()))
    return get_match(match_id, request)


@router.get("/{match_id}", summary="What I should see now", responses=errors(404))
def get_match(match_id: MatchId, request: Request) -> MatchState:
    """The match as this player sees it at this moment, worked out from the server's clock: the phase, the open
    question or turn, time limits, and everything that may be revealed so far. Poll about once a second. A
    question's answer is only included once its time is over for everyone."""
    doc = memory_of(request).get_match(player_id(), match_id)
    if doc is None:
        raise gone()
    return MatchState(**matches.state(doc, player_id(), matches.clock()))


@router.post(
    "/{match_id}/answer", summary="Answer the open question (mcq)", responses=errors(404, 409, 422)
)
def answer(match_id: MatchId, body: AnswerBody, request: Request) -> dict:
    """Your first answer to the question that is open now is final. Late answers (after your own time, which is
    shorter for a parent) get 409. Whether it was right is shown to everyone when the question closes."""
    pid = player_id()

    def change(doc: dict) -> dict:
        is_game(doc, "mcq")
        return matches.mcq_answer(doc, pid, body.question_index, body.choice, matches.clock())

    return play(memory_of(request), match_id, change)


@router.post(
    "/{match_id}/cards", summary="Mark the current card (heads_up)", responses=errors(403, 404, 409)
)
def mark_card(match_id: MatchId, body: CardBody, request: Request) -> dict:
    """During a turn, the guesser or the explainer marks the card shown now: `correct` (+1), `skip` (0) or `violation`
    (-1). The next card is dealt. Sending the same card twice is a harmless replay; a card not dealt yet is a 409.
    The card itself is in `GET /api/matches/{id}` for the two players of the turn."""
    pid = player_id()

    def change(doc: dict) -> dict:
        is_game(doc, "heads_up")
        return matches.hu_action(doc, pid, body.action, body.card_index, matches.clock())

    return play(memory_of(request), match_id, change)


@router.put(
    "/{match_id}/placements",
    summary="Place words in the blanks (diagram)",
    responses=errors(404, 409, 422),
)
def place_words(match_id: MatchId, body: PlacementBody, request: Request) -> dict:
    """Send your whole arrangement as slot id to word index; it replaces the previous one. Partial is fine."""
    pid = player_id()

    def change(doc: dict) -> dict:
        is_game(doc, "diagram")
        return matches.dg_place(doc, pid, body.placements, matches.clock())

    return play(memory_of(request), match_id, change)


@router.post("/{match_id}/finish", summary="Check my diagram (diagram)", responses=errors(404, 409))
def finish_diagram(match_id: MatchId, request: Request) -> dict:
    """Every blank must be filled. A wrong finish returns how many are right and which slots are wrong (never the
    right words), adds a time penalty to your result, and the clock keeps running. A perfect one records your time."""
    pid = player_id()

    def change(doc: dict) -> dict:
        is_game(doc, "diagram")
        return matches.dg_finish(doc, pid, matches.clock())

    return play(memory_of(request), match_id, change)


@router.get("/{match_id}/results", summary="The ranking", responses=errors(404, 409))
def get_results(match_id: MatchId, request: Request) -> Results:
    """Who won, once the game is over (409 before). Equal results share a rank. For multiple choice: most correct, then
    less time on the correct ones. For heads-up: most points. For the diagram: only perfect reconstructions are
    ranked, by time plus the penalty for each wrong finish."""
    doc = memory_of(request).get_match(player_id(), match_id)
    if doc is None:
        raise gone()
    try:
        return Results(**matches.results(doc, matches.clock()))
    except MatchError as exc:
        raise HTTPException(exc.status, exc.message) from exc


@router.get(
    "/{match_id}/diagram/view",
    response_class=HTMLResponse,
    summary="View the masked diagram board",
    responses=errors(404, 409),
)
def diagram_view(match_id: MatchId, request: Request):
    """Render only the public masked diagram for a captured participant. The private slot answers
    remain server-side; the response must stay in a sandboxed iframe."""
    from learning_service.visuals.models import Visual
    from learning_service.visuals.render import document

    doc = memory_of(request).get_match(player_id(), match_id)
    if doc is None:
        raise gone()
    if doc["game"] != "diagram":
        raise HTTPException(409, "This match has no diagram board.")
    code = doc["mermaid"]
    for slot in doc["slots"]:
        code = code.replace(f'{slot}["?"]', f'{slot}["{slot}: ?"]')
    visual = Visual(
        kind="diagram",
        section_index=0,
        title=doc["title"],
        caption="Fill the missing labels.",
        alt="Diagram with missing labels",
        code=code,
    )
    return HTMLResponse(
        document(visual, match_id), headers={"Content-Security-Policy": "sandbox allow-scripts"}
    )
