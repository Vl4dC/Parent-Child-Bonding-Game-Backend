"""The current frontend's /api/social contract, with verified Clerk sessions."""

from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.clerk_social import social_context
from app.generation import generation_permit
from app.repository import current_session
from learning_service import store
from learning_service.live_social import LiveSocial, SocialError
from learning_service.store import PublicQuestion


class ErrorOut(BaseModel):
    detail: str
    code: str | None = None


router = APIRouter(
    prefix="/api/social",
    tags=["Social"],
    dependencies=[Depends(social_context, scope="function")],
    responses={
        401: {"model": ErrorOut, "description": "Missing/invalid Clerk session"},
        403: {"model": ErrorOut, "description": "Player mapping or participant rule rejected"},
        404: {"model": ErrorOut, "description": "User, request, lesson or duel not found"},
        409: {"model": ErrorOut, "description": "State changed, offline or busy"},
        429: {"model": ErrorOut, "description": "Request/generation limit reached"},
        503: {
            "model": ErrorOut,
            "description": "Storage, Clerk verification or generation unavailable",
        },
    },
)
Id = Annotated[str, Path(min_length=1, max_length=128)]
Avatar = Literal["nova", "mia", "zara", "leo", "sam", "mom", "dad"]
Role = Literal["learner", "parent"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileIn(Input):
    display_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)
    ]
    avatar: Avatar = "nova"
    role: Role = "learner"


class ProfileOut(ProfileIn):
    id: str
    username: str


class PublicUser(BaseModel):
    id: str
    username: str
    display_name: str
    avatar: Avatar
    online: bool | None
    busy: bool | None


class SearchUser(PublicUser):
    relationship: Literal["none", "friend", "request_sent", "request_received"]


class SearchOut(BaseModel):
    users: list[SearchUser]


class Friend(PublicUser):
    since: datetime


class FriendRequest(BaseModel):
    id: str
    from_: PublicUser = Field(alias="from")
    to: PublicUser
    created_at: datetime


class FriendsOut(BaseModel):
    friends: list[Friend]
    incoming: list[FriendRequest]
    outgoing: list[FriendRequest]


class FriendRequestIn(Input):
    username: str = Field(min_length=1, max_length=65)


class SentRequest(BaseModel):
    status: Literal["sent"]
    request: FriendRequest


class AcceptedRequest(BaseModel):
    status: Literal["accepted"]
    friend: PublicUser


class FriendOut(BaseModel):
    friend: PublicUser


class DuelIn(Input):
    opponent_id: str = Field(min_length=1, max_length=128)
    lesson_id: str = Field(min_length=1, max_length=128)
    num_questions: int = Field(5, ge=3, le=8, strict=True)


class AnswerIn(Input):
    index: int = Field(ge=0, strict=True)
    choice: int = Field(ge=0, strict=True)


class AnswerOut(BaseModel):
    answered: int


class DuelResult(BaseModel):
    index: int
    choice: int | None
    correct: bool
    answer_index: int
    explanation: str


class Sheet(BaseModel):
    score: int
    total: int
    results: list[DuelResult]


class Results(BaseModel):
    me: Sheet
    opponent: Sheet
    winner: Literal["me", "opponent", "draw"]


class Progress(BaseModel):
    me: int
    opponent: int


class Duel(BaseModel):
    id: str
    status: Literal["invited", "declined", "expired", "cancelled", "active", "finished"]
    my_role: Literal["challenger", "opponent"]
    challenger: PublicUser
    opponent: PublicUser
    lesson_title: str
    num_questions: int
    created_at: datetime
    invite_expires_at: datetime
    start_at: datetime | None
    ends_at: datetime | None
    server_now: datetime
    questions: list[PublicQuestion] | None
    progress: Progress
    results: Results | None


class PresenceIn(Input):
    state: Literal["online", "offline"] = "online"


class PresenceOut(BaseModel):
    heartbeat_s: int
    online_window_s: int
    invites: list[Duel]


ProfileBody = Annotated[
    ProfileIn,
    Body(
        openapi_examples={
            "profile": {
                "summary": "Register/update the signed-in user; username comes from JWT",
                "value": {"display_name": "Alex", "avatar": "nova", "role": "learner"},
            }
        }
    ),
]
PresenceBody = Annotated[
    PresenceIn,
    Body(
        openapi_examples={
            "online": {
                "summary": "Renew presence and fetch incoming duel invites",
                "value": {"state": "online"},
            },
            "offline": {
                "summary": "End this Clerk session's presence",
                "value": {"state": "offline"},
            },
        }
    ),
]
FriendBody = Annotated[
    FriendRequestIn,
    Body(
        openapi_examples={
            "friend": {
                "summary": "Find an exact registered username",
                "value": {"username": "zara_k"},
            }
        }
    ),
]
DuelBody = Annotated[
    DuelIn,
    Body(
        openapi_examples={
            "duel": {
                "summary": "Generate a five-question live duel from my lesson",
                "value": {
                    "opponent_id": "user_friend",
                    "lesson_id": "<lesson id>",
                    "num_questions": 5,
                },
            }
        }
    ),
]
AnswerBody = Annotated[
    AnswerIn,
    Body(
        openapi_examples={
            "answer": {
                "summary": "Answer question zero with option one",
                "value": {"index": 0, "choice": 1},
            }
        }
    ),
]


def service(request):
    return LiveSocial(request.app.state.home)


def actor(request):
    return request.state.social_identity


@router.put("/me", response_model=ProfileOut)
def me(body: ProfileBody, request: Request):
    """Bind the signed Clerk subject/player UUID and save public profile metadata. Username is never accepted in the body."""
    return service(request).save_profile(actor(request), body.model_dump())


@router.post("/presence", response_model=PresenceOut)
def presence(body: PresenceBody, request: Request):
    """20-second heartbeat, 45-second online window, with current incoming live-duel invitations."""
    return service(request).heartbeat(actor(request), body.state)


@router.get("/users/search", response_model=SearchOut)
def search(
    request: Request,
    q: str = Query(min_length=2, max_length=64),
    limit: int = Query(10, ge=1, le=20),
):
    """Case-insensitive username prefix search, excluding me. Presence is null for nonfriends."""
    q = q.strip()
    if len(q) < 2:
        raise SocialError(422, "Type at least two username characters.")
    return service(request).search(actor(request), q, limit)


@router.get("/friends", response_model=FriendsOut)
def friends(request: Request, online: bool = False):
    """Friends sorted online first, plus incoming and outgoing opaque friend requests."""
    return service(request).friends(actor(request), online)


@router.post(
    "/friend-requests",
    response_model=SentRequest | AcceptedRequest,
    status_code=201,
    responses={
        200: {"model": AcceptedRequest, "description": "Mutual requests establish friendship"}
    },
)
def send_request(body: FriendBody, request: Request, response: Response):
    """Send by username; duplicate/already-accepted pairs return 409, mutual requests return 200."""
    status, result = service(request).send_request(actor(request), body.username)
    response.status_code = status
    return result


@router.post("/friend-requests/{id}/accept", response_model=FriendOut)
def accept_request(id: Id, request: Request):
    """Only the recipient accepts this exact request; stale or wrong-party IDs return 404."""
    return service(request).answer_request(actor(request), id, "accept")


@router.post("/friend-requests/{id}/decline", status_code=204)
def decline_request(id: Id, request: Request):
    """Only the recipient declines the pending request. Its opaque ID cannot affect a later request."""
    service(request).answer_request(actor(request), id, "decline")


@router.delete("/friend-requests/{id}", status_code=204)
def cancel_request(id: Id, request: Request):
    """Only the sender cancels the pending request; existing friendships are not removed."""
    service(request).answer_request(actor(request), id, "cancel")


@router.delete("/friends/{user_id}", status_code=204)
def unfriend(user_id: Annotated[str, Path(min_length=1, max_length=128)], request: Request):
    """Remove this friend by public Clerk subject ID. Existing game results remain available."""
    service(request).unfriend(actor(request), user_id)


@router.post(
    "/duels",
    response_model=Duel,
    status_code=201,
    responses={502: {"description": "Question generation failed"}},
)
def create_duel(body: DuelBody, request: Request):
    """Generate from the challenger's selected owned lesson. Recheck both players after generation.
    This casual duel does not change the lesson's approval state. The answer key stays server-side."""
    live, identity = service(request), actor(request)
    lesson = store.get_or_404(store.LESSONS, body.lesson_id, "Lesson")
    ticket, other = live.reserve_duel(identity, body.opponent_id)
    try:
        with contextmanager(generation_permit)(request):
            quiz = store.quiz_from_lesson(lesson, body.num_questions)
        quiz.questions = quiz.questions[: body.num_questions]
        if not quiz.questions:
            raise SocialError(502, "No usable questions were generated.")
        questions = [q.model_dump(mode="json") for q in quiz.public().questions]
        try:
            key = [
                {"answer_index": q.options.index(q.answer), "explanation": q.explanation}
                for q in quiz.questions
            ]
        except ValueError as exc:
            raise SocialError(502, "The generated questions could not be scored.") from exc
        unit = current_session.get()
        if not unit.connection:
            with unit.repository.lock:
                if (
                    unit.repository.data.get((unit.owner, "lessons", lesson.id))
                    != unit.original[("lessons", lesson.id)]
                ):
                    raise SocialError(
                        409, "The source lesson changed while the questions were generated."
                    )
        return live.create_duel(identity, other, ticket, lesson, questions, key)
    finally:
        live.release(ticket)


@router.get("/duels/current", response_model=Duel)
def current_duel(request: Request):
    """Return my invited/active duel, or 404 when none remains after server-clock settlement."""
    return service(request).get_duel(actor(request))


@router.get("/duels/{id}", response_model=Duel)
def get_duel(id: Id, request: Request):
    """Poll progress and timing. Questions unlock after countdown; results unlock only when finished."""
    return service(request).get_duel(actor(request), id)


@router.post("/duels/{id}/accept", response_model=Duel)
def accept_duel(id: Id, request: Request):
    """Opponent accepts before expiry; both players receive the same five-second countdown."""
    return service(request).action(actor(request), id, "accept")


@router.post("/duels/{id}/decline", response_model=Duel)
def decline_duel(id: Id, request: Request):
    """Opponent declines while invited. No score, participation coins or quest progress is earned."""
    return service(request).action(actor(request), id, "decline")


@router.post("/duels/{id}/cancel", response_model=Duel)
def cancel_duel(id: Id, request: Request):
    """Challenger cancels a still-pending invitation. Active or terminal duels cannot be cancelled."""
    return service(request).action(actor(request), id, "cancel")


@router.post("/duels/{id}/answers", response_model=AnswerOut)
def answer(id: Id, body: AnswerBody, request: Request):
    """Record a player's first choice. Exact replays return the saved acknowledgement, including after finish.
    No correctness or answer key is returned before the shared results screen."""
    return service(request).answer(actor(request), id, body.index, body.choice)
