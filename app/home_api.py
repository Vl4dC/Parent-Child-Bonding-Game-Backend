"""
The home: rooms built, item levels and positions, the wallet that pays for them, friends, visits and cheers.

  GET  /api/me/home                                    my home, my coins, its version
  POST /api/me/home/rooms                              build the next room
  POST /api/me/home/items/{room}/{item}/place          place an item
  POST /api/me/home/items/{room}/{item}/upgrade        raise its level by one
  PUT  /api/me/home/rooms/{room}/layout                replace a room's layout (free)
  PUT  /api/me/home/visibility                         who may visit
  PUT  /api/me/home/card                               the name and avatar friends see
  GET  /api/me/friends                                 my friend code, friends and pending requests
  POST /api/me/friends/requests                        ask a player (by their friend code) to be friends
  POST /api/me/friends/requests/{player_id}/accept|decline   answer a request sent by that player
  DELETE /api/me/friends/{player_id}                   remove a friend or cancel a request
  GET  /api/homes?scope=friends|family                 cards of homes I may visit
  GET  /api/homes/{player_id}                          visit a home (read-only, no coins)
  POST /api/homes/{player_id}/cheers                   cheer a room or the whole home, once a day

Data is kept per player (the sign-in token's `player_id`) in a SQLite file (learning_service/home_store.py).
Friends and `visibility` are an advisory privacy setting, not a family or consent check.
Every change may carry `expected_version`; a stale one is a 409 and changes nothing.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.context import api_context
from app.repository import current_session
from learning_service.home_rules import RULES
from learning_service.home_store import (
    Friend,
    Home,
    HomeCard,
    HomeError,
    HomeStore,
    MyHome,
    Placement,
    Visibility,
)

router = APIRouter(
    prefix="/api", tags=["Home"], dependencies=[Depends(api_context, scope="function")]
)

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
Version = Annotated[
    int | None,
    Field(ge=1, description="The home `version` you last saw. A newer one is a 409"),
]
CHANGE_ERRORS = {
    402: {"description": "Not enough coins"},
    404: {"description": "No such room or item"},
    409: {"description": "`expected_version` is out of date: reload the home"},
    422: {"description": "A rule is broken (the message says which)"},
}


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChangeIn(Body):
    expected_version: Version = None


class PlaceIn(Placement):
    model_config = ConfigDict(extra="forbid")
    expected_version: Version = None


class LayoutIn(Body):
    items: dict[str, Placement] = Field(
        ...,
        description="Placed items to put somewhere. Items left out return to their default spot",
    )
    expected_version: Version = None


class VisibilityIn(Body):
    visibility: Visibility = Field(
        ...,
        description="`friends` (accepted friends may visit), `family` or `nobody`. "
        "No family link exists yet, so `family` admits only you",
    )
    expected_version: Version = None


class CardIn(Body):
    display_name: Name
    avatar: str | None = Field(None, max_length=128, description="An avatar reference, e.g. `zara`")


def store_of(request: Request) -> HomeStore:
    return request.app.state.home


def me() -> str:
    return current_session.get().owner


@contextmanager
def reporting():
    """Answer a broken rule with its HTTP status."""
    try:
        yield
    except HomeError as error:
        raise HTTPException(error.status, error.message) from None


@router.get("/me/home", response_model=MyHome, summary="Read my home, coins and version")
def my_home(request: Request):
    """The whole home in one response: built rooms, each placed item's level and position, the coin balance and the
    cheers received. The first call creates the living room with its starter items and the starter coins."""
    return store_of(request).get_home(me())


@router.post(
    "/me/home/rooms",
    response_model=MyHome,
    responses=CHANGE_ERRORS,
    summary="Build the next room",
)
def build_room(request: Request, body: ChangeIn):
    """Builds the next room in order (kitchen, bedroom, bathroom) once every item of the newest room is at level 3
    or higher. It costs 250 coins times the rooms already built. Its starter items begin at level 1."""
    with reporting():
        return store_of(request).build_room(me(), body.expected_version)


@router.post(
    "/me/home/items/{room_id}/{item_id}/place",
    response_model=MyHome,
    responses=CHANGE_ERRORS,
    summary="Place an item",
)
def place_item(request: Request, room_id: str, item_id: str, body: PlaceIn):
    """Places an item that is not there yet, at level 1, for 30 coins. Starter items are already placed. `at` is
    where the app put it (floor tiles, 0 to 2.7) or null for its default spot."""
    with reporting():
        return store_of(request).place_item(
            me(), room_id, item_id, Placement(at=body.at, rot=body.rot), body.expected_version
        )


@router.post(
    "/me/home/items/{room_id}/{item_id}/upgrade",
    response_model=MyHome,
    responses=CHANGE_ERRORS,
    summary="Raise an item's level by one",
)
def upgrade_item(request: Request, room_id: str, item_id: str, body: ChangeIn):
    """Level n costs 20 coins times n (level 2 is 40, level 10 is 200). The top level is 10."""
    with reporting():
        return store_of(request).upgrade_item(me(), room_id, item_id, body.expected_version)


@router.put(
    "/me/home/rooms/{room_id}/layout",
    response_model=MyHome,
    responses=CHANGE_ERRORS,
    summary="Replace a room's layout",
)
def set_layout(request: Request, room_id: str, body: LayoutIn):
    """Saves where the player moved and turned items in one room. Free. The server checks that the items are placed
    and the positions are on the floor; it does not check furniture overlap, which the app resolves when drawing."""
    with reporting():
        return store_of(request).set_layout(me(), room_id, body.items, body.expected_version)


@router.put(
    "/me/home/visibility",
    response_model=MyHome,
    responses=CHANGE_ERRORS,
    summary="Choose who may visit",
)
def set_visibility(request: Request, body: VisibilityIn):
    """An advisory privacy setting: callers are not authenticated, so this is not access control."""
    with reporting():
        return store_of(request).set_visibility(me(), body.visibility, body.expected_version)


@router.put("/me/home/card", response_model=MyHome, summary="Set the name and avatar friends see")
def set_card(request: Request, body: CardIn):
    """Shown on friend lists, home cards and visits. It does not change the home's `version`."""
    return store_of(request).set_card(me(), body.display_name, body.avatar)


class FriendRequestIn(Body):
    code: Annotated[
        str, StringConstraints(strip_whitespace=True, to_upper=True, min_length=4, max_length=16)
    ] = Field(..., description="The other player's friend code (not case sensitive)")


class FriendList(BaseModel):
    code: str = Field(..., description="My friend code: share it so others can add me")
    items: list[Friend]


class HomeList(BaseModel):
    items: list[HomeCard]


class CheerIn(Body):
    room_id: str = Field(
        "home", max_length=32, description="A built room id, or `home` for the whole home"
    )


class CheerOut(BaseModel):
    room_id: str
    day: str


@router.get(
    "/me/friends", response_model=FriendList, summary="My friend code, friends and requests"
)
def my_friends(request: Request):
    """`status` is `accepted`, `incoming` (they asked me) or `outgoing` (I asked them). Players are not
    authenticated, so a friend is whoever shared a code, not a verified identity."""
    store = store_of(request)
    return FriendList(code=store.friend_code(me()), items=store.list_friends(me()))


@router.post(
    "/me/friends/requests",
    response_model=Friend,
    responses={
        404: {"description": "No player has that code"},
        422: {"description": "Your own code"},
    },
    summary="Ask a player to be friends",
)
def request_friend(request: Request, body: FriendRequestIn):
    """Repeating a request changes nothing. If they already asked me, we become friends at once."""
    with reporting():
        return store_of(request).request_friend(me(), body.code)


@router.post(
    "/me/friends/requests/{player_id}/accept",
    response_model=Friend,
    responses={404: {"description": "That player sent no request"}},
    summary="Accept a friend request",
)
def accept_friend(request: Request, player_id: UUID):
    """`player_id` is the player who sent the request."""
    with reporting():
        return store_of(request).respond(me(), str(player_id), True)


@router.post(
    "/me/friends/requests/{player_id}/decline",
    status_code=204,
    responses={404: {"description": "That player sent no request"}},
    summary="Decline a friend request",
)
def decline_friend(request: Request, player_id: UUID):
    """`player_id` is the player who sent the request. The request is deleted."""
    with reporting():
        store_of(request).respond(me(), str(player_id), False)


@router.delete(
    "/me/friends/{player_id}",
    status_code=204,
    responses={404: {"description": "Not a friend and no request"}},
    summary="Remove a friend or cancel a request",
)
def remove_friend(request: Request, player_id: UUID):
    """Works for an accepted friend and for a pending request in either direction."""
    if not store_of(request).remove_friend(me(), str(player_id)):
        raise HTTPException(404, "There is no such friend.")


@router.get("/homes", response_model=HomeList, summary="Homes I may visit")
def list_homes(request: Request, scope: Literal["friends", "family"] = "friends"):
    """Cards (name, avatar, rooms built, levels) of accepted friends who let friends visit. `family` is empty until
    a family link exists."""
    return HomeList(items=store_of(request).list_homes(me(), scope))


@router.get(
    "/homes/{player_id}",
    response_model=Home,
    responses={
        404: {"description": "No such home, or it is not open to me (the same answer for both)"}
    },
    summary="Visit a home",
)
def visit_home(request: Request, player_id: UUID):
    """Read-only, and without coins. A stranger, a private home and a missing one all get the same 404."""
    with reporting():
        return store_of(request).visit(me(), str(player_id))


@router.post(
    "/homes/{player_id}/cheers",
    status_code=201,
    response_model=CheerOut,
    responses={
        404: {"description": "No such home, or it is not open to me"},
        409: {"description": "Already cheered there today"},
        422: {"description": "Your own home, or a room that is not built"},
    },
    summary="Cheer a room or a home",
)
def cheer_home(request: Request, player_id: UUID, body: CheerIn):
    """One cheer per room (or the whole home) per visitor per day. Only where a visit is allowed."""
    with reporting():
        day = store_of(request).cheer(me(), str(player_id), body.room_id)
    return CheerOut(room_id=body.room_id, day=day)


class ClaimQuizIn(Body):
    quiz_id: str = Field(..., min_length=1, max_length=128, description="A quiz `id` I own")


class CoinsOut(BaseModel):
    coins: int = Field(..., description="The balance now")
    credited: int = Field(..., description="Coins added by this call (0 if all were paid before)")


class TopupIn(Body):
    amount: int = Field(500, ge=1, le=100_000)


@router.post(
    "/me/coins/claim-quiz",
    response_model=CoinsOut,
    responses={404: {"description": "No such quiz for this player"}},
    summary="Collect coins for a quiz's correct answers",
)
def claim_quiz(request: Request, body: ClaimQuizIn):
    """Pays 10 coins for each question I got right on my first try, read from the answers the server recorded. Each
    question of a question set pays once, so claiming again, or retrying the same questions, adds nothing."""
    from app.quiz_practice import quiz_for

    quiz = quiz_for(body.quiz_id)
    set_id = quiz.question_set_id or quiz.id
    entries = [
        (f"{set_id}/{index}", RULES.quiz_coins_per_correct)
        for index, attempt in sorted(quiz.attempts.items())
        if attempt.correct
    ]
    coins, credited = store_of(request).credit(me(), "quiz", entries)
    return CoinsOut(coins=coins, credited=credited)


@router.post(
    "/me/coins/demo-topup",
    response_model=CoinsOut,
    responses={404: {"description": "Not in demo mode"}},
    summary="Add coins to try the home out (demo mode only)",
)
def demo_topup(request: Request, body: TopupIn):
    """Replaces the app's "unlimited coins" test tool. It is a 404 unless the server runs with `DEMO_MODE=true`."""
    if not request.app.state.settings.demo_mode:
        raise HTTPException(404, "Not found.")
    coins, credited = store_of(request).credit(
        me(), "demo_topup", [(uuid.uuid4().hex, body.amount)]
    )
    return CoinsOut(coins=coins, credited=credited)
