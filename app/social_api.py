"""Friend presence and game invitations; no frontend, auth provider, or message delivery service."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app import game_invites
from app.context import api_context
from app.home_api import me, reporting, store_of
from app.rooms import RoomSnapshot
from learning_service import presence
from learning_service.home_store import Friend

router = APIRouter(
    prefix="/api/me",
    tags=["Friends and invitations"],
    dependencies=[Depends(api_context, scope="function")],
    responses={
        404: {"description": "Friend/invitation/lobby not found"},
        409: {"description": "Invitation or lobby state changed"},
        429: {"description": "Presence or invitation limit reached"},
    },
)
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]
Game = Literal["mcq", "heads_up", "diagram"]
Mode = Literal["friends", "parent_child", "duo"]
Status = Literal["pending", "accepted", "declined", "cancelled", "expired"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SocialSettings(Input):
    share_presence: bool = True
    allow_game_invites: bool = True


class HeartbeatIn(Input):
    status: Literal["online", "away", "busy"] = "online"


class HeartbeatOut(BaseModel):
    session_id: UUID
    expires_at: datetime
    heartbeat_after_seconds: int
    server_now: datetime


class FriendPresence(BaseModel):
    player_id: UUID
    display_name: str
    avatar: str | None
    status: Literal["online", "away", "busy", "offline"]
    last_seen: datetime | None


class PresenceList(BaseModel):
    items: list[FriendPresence]
    server_now: datetime
    poll_after_seconds: int


class InviteIn(Input):
    friend_id: UUID
    room_id: UUID
    game: Game = "mcq"
    mode: Mode = "friends"


class InviteOut(BaseModel):
    id: UUID
    sender_id: UUID
    recipient_id: UUID
    room_id: UUID
    game: Game
    mode: Mode
    status: Status
    created_at: datetime
    expires_at: datetime
    updated_at: datetime


class InvitePage(BaseModel):
    items: list[InviteOut]
    next_cursor: UUID | None
    server_now: datetime
    poll_after_seconds: int


class InviteAction(BaseModel):
    invite: InviteOut
    room: RoomSnapshot | None = None


SettingsBody = Annotated[
    SocialSettings,
    Body(
        openapi_examples={
            "visible": {
                "summary": "Share status with friends and allow invitations",
                "value": {"share_presence": True, "allow_game_invites": True},
            },
            "quiet": {
                "summary": "Hide status and stop new invitations",
                "value": {"share_presence": False, "allow_game_invites": False},
            },
        }
    ),
]
HeartbeatBody = Annotated[
    HeartbeatIn,
    Body(
        openapi_examples={
            "online": {"summary": "Renew this tab's presence lease", "value": {"status": "online"}},
            "away": {"summary": "This tab is inactive", "value": {"status": "away"}},
        }
    ),
]
InviteBody = Annotated[
    InviteIn,
    Body(
        openapi_examples={
            "duel": {
                "summary": "Invite a friend into a two-player quiz lobby",
                "value": {
                    "friend_id": "00000000-0000-4000-8000-000000000002",
                    "room_id": "00000000-0000-4000-8000-000000000003",
                    "game": "mcq",
                    "mode": "friends",
                },
            },
            "group": {
                "summary": "Invite a friend into a heads-up lobby",
                "value": {
                    "friend_id": "00000000-0000-4000-8000-000000000002",
                    "room_id": "00000000-0000-4000-8000-000000000003",
                    "game": "heads_up",
                    "mode": "friends",
                },
            },
        }
    ),
]


@router.get("/social-settings", response_model=SocialSettings)
def get_settings(request: Request):
    return presence.settings(store_of(request), me())


@router.put("/social-settings", response_model=SocialSettings)
def set_settings(body: SettingsBody, request: Request):
    """Replace social preferences. Hiding presence also hides last-seen timestamps from friends."""
    return presence.put_settings(store_of(request), me(), body.model_dump())


@router.put("/presence/sessions/{session_id}", response_model=HeartbeatOut)
def heartbeat(session_id: UUID, body: HeartbeatBody, request: Request):
    """Use one UUID per tab/device. Server timestamps and a renewable TTL determine online status."""
    return presence.heartbeat(
        store_of(request),
        me(),
        str(session_id),
        body.status,
        request.app.state.settings.presence_ttl_seconds,
    )


@router.delete("/presence/sessions/{session_id}", status_code=204)
def disconnect(session_id: UUID, request: Request):
    """Idempotently end this tab's lease; other live sessions stay online."""
    presence.disconnect(store_of(request), me(), str(session_id))


@router.get("/friends/presence", response_model=PresenceList)
def friends_presence(request: Request):
    """Accepted friends only. Hidden/stale status is offline; hidden last-seen is always null."""
    return presence.friends(
        store_of(request), me(), request.app.state.settings.presence_ttl_seconds
    )


@router.post("/game-invites", status_code=201, response_model=InviteOut)
def invite(body: InviteBody, key: Key, request: Request):
    """Only the current lobby host invites accepted friends. Offline friends may be invited too.
    No membership is reserved and no game/content generation is started."""
    return game_invites.create(
        store_of(request), body.model_dump(mode="json"), key, request.app.state.settings
    )


@router.get("/game-invites", response_model=InvitePage)
def invitations(
    direction: Literal["incoming", "outgoing", "all"] = "all",
    status: Status | None = None,
    after: UUID | None = None,
    limit: int = Query(50, ge=1, le=100),
):
    store = game_invites.Store()
    with store.transaction():
        return store.page(direction, status, str(after) if after else None, limit)


@router.get("/game-invites/{invite_id}", response_model=InviteOut)
def invitation(invite_id: UUID):
    store = game_invites.Store()
    with store.transaction():
        return game_invites.public(store.get(str(invite_id)))


@router.post("/game-invites/{invite_id}/accept", response_model=InviteAction)
def accept(invite_id: UUID, request: Request):
    """Recipient accepts and joins the still-open lobby atomically in PostgreSQL. Replays never rejoin after leaving."""
    return game_invites.act(store_of(request), str(invite_id), "accept")


@router.post("/game-invites/{invite_id}/decline", response_model=InviteAction)
def decline(invite_id: UUID, request: Request):
    return game_invites.act(store_of(request), str(invite_id), "decline")


@router.post("/game-invites/{invite_id}/cancel", response_model=InviteAction)
def cancel(invite_id: UUID, request: Request):
    return game_invites.act(store_of(request), str(invite_id), "cancel")


class FriendRequestIn(Input):
    friend_id: UUID | None = None
    code: str | None = Field(None, min_length=8, max_length=8, pattern=r"^[A-Za-z0-9]{8}$")

    @model_validator(mode="after")
    def one_target(self):
        if (self.friend_id is None) == (self.code is None):
            raise ValueError("Supply exactly one friend_id or friend code")
        return self


class PendingFriend(Friend):
    requested_at: datetime


class FriendRequests(BaseModel):
    incoming: list[PendingFriend]
    outgoing: list[PendingFriend]
    server_now: datetime
    poll_after_seconds: int = 15


FriendRequestBody = Annotated[
    FriendRequestIn,
    Body(
        openapi_examples={
            "code": {
                "summary": "Send a request using your friend's code",
                "value": {"code": "ABCD2345"},
            },
            "player": {
                "summary": "Send a request to a known player UUID",
                "value": {"friend_id": "00000000-0000-4000-8000-000000000002"},
            },
        }
    ),
]


@router.get("/friend-requests", response_model=FriendRequests)
def friend_requests(request: Request):
    """Separate incoming and outgoing requests with timestamps; accepted friends use /me/friends."""
    return store_of(request).pending_friend_requests(me()) | {
        "server_now": presence.stamp(presence.clock())
    }


@router.post("/friend-requests", response_model=Friend)
def request_friend(body: FriendRequestBody, request: Request):
    """Send by known player UUID or friend code. Replays do not duplicate requests.
    If the other player already requested you, mutual requests establish friendship."""
    with reporting():
        if body.friend_id is not None:
            return store_of(request).request_friend_id(me(), str(body.friend_id))
        return store_of(request).request_friend(me(), body.code)


@router.post("/friend-requests/{player_id}/accept", response_model=Friend)
def accept_friend(player_id: UUID, request: Request):
    """Accept the selected player's incoming request; already-accepted replays return the friend."""
    with reporting():
        return store_of(request).answer_friend_request(me(), str(player_id), "accept")


@router.post("/friend-requests/{player_id}/decline", status_code=204)
def decline_friend(player_id: UUID, request: Request):
    """Decline an incoming request. A missing/already-declined request is a harmless replay."""
    with reporting():
        store_of(request).answer_friend_request(me(), str(player_id), "decline")


@router.delete("/friend-requests/{player_id}", status_code=204)
def cancel_friend_request(player_id: UUID, request: Request):
    """Cancel an outgoing request only. Accepted friendships require the existing remove-friend route."""
    with reporting():
        store_of(request).answer_friend_request(me(), str(player_id), "cancel")
