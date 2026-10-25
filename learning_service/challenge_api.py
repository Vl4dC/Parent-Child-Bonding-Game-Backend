"""
Friend challenges over HTTP: find what friends share, make a game from it, read it without the answers.

  GET    /api/challenges/shared-topics?room_id=   topics every member of the room has studied
  POST   /api/challenges                          make a game (mcq, heads_up or diagram) for a topic
  GET    /api/challenges/{id}                     the public view: no answers (the creator and the room's members)
  GET    /api/challenges/{id}/review              the full content with answers (the creator only)
  DELETE /api/challenges/{id}                     forget it (the creator only)

The play engines (timers, scoring, who wins) come later; this is the frozen content they will run. See challenges.py.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field, StringConstraints

from app.generation import generation_permit
from app.repository import current_session
from app.rooms import command as room_command
from learning_service import challenges as ch
from learning_service.api_docs import CHALLENGES, ChallengeId, errors
from learning_service.challenges import (
    CASUAL_LABEL,
    ChallengeFailed,
    DiagramContent,
    DiagramPublic,
    Game,
    HeadsUpContent,
    HeadsUpPublic,
    McqContent,
    McqPublic,
    SharedTopic,
)
from learning_service.memory import Subject
from learning_service.memory_store import MemoryStore, StoredChallenge
from learning_service.safety import AgeBlocked, SafetyUnavailable

log = logging.getLogger("challenge_api")
router = APIRouter(prefix="/api/challenges", tags=[CHALLENGES])


class NewChallenge(BaseModel):
    game: Game = Field(..., description="`mcq`, `heads_up` or `diagram`")
    topic: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
    ] = Field(..., description="What the game is about, for example one of the shared topics")
    age: int = Field(
        ...,
        ge=5,
        le=99,
        description="Use the lowest age among the players, so the game is fair and suitable for everyone",
    )
    room_id: str | None = Field(
        None,
        description="Share the challenge with everyone in this room (you must be a member). Leave out for solo.",
    )
    subject: Subject | None = Field(
        None, description="The subject, if you got the topic from shared-topics"
    )
    count: int | None = Field(
        None,
        ge=1,
        le=30,
        description="Questions (mcq, 5 to 15, default 10) or terms (heads_up, 8 to 30, default 20)",
    )
    seconds: int | None = Field(
        None,
        ge=1,
        le=600,
        description="mcq: seconds per question (15 to 60, default 30). heads_up: seconds per turn (30 to 120, "
        "default 60). diagram: seconds for the whole diagram (60 to 300, default 120).",
    )


class ChallengeOut(BaseModel):
    id: str
    game: Game
    topic: str
    subject: str | None
    age: int
    label: str = Field(
        CASUAL_LABEL, description="Show this: the content is AI-written and not reviewed"
    )
    casual: bool = True
    players: int = Field(..., description="How many people can read this challenge")
    created_at: str
    expires_at: str
    content: McqPublic | HeadsUpPublic | DiagramPublic = Field(
        ..., description="The game without its answers. Its shape depends on `game`"
    )


class ChallengeReview(ChallengeOut):
    content: McqContent | HeadsUpContent | DiagramContent = Field(
        ...,
        description="The full content including answers, for the creator and the grading engine",
    )


class SharedPlayer(BaseModel):
    player_id: str
    display_name: str
    role: str | None = Field(None, description="`parent` or `learner` if they set a memory profile")


class SharedTopics(BaseModel):
    room_id: str
    players: list[SharedPlayer]
    topics: list[SharedTopic] = Field(
        ...,
        description="Topics every player studied, most recent first. Nothing else about anyone's memory is shown",
    )


def memory_of(request: Request) -> MemoryStore:
    return request.app.state.memory


def player_id() -> str:
    return current_session.get().owner


def room_members(room_id: str) -> list[dict]:
    """The room's members, if the caller is one (404 otherwise)."""
    room = room_command("snapshot", room_id)
    members = room["members"]
    if player_id() not in [m["user_id"] for m in members]:
        raise HTTPException(404, "Room not found.")
    return members


def out(challenge: StoredChallenge, review: bool = False) -> ChallengeOut:
    base = dict(
        id=challenge.id,
        game=challenge.game,
        topic=challenge.topic,
        subject=challenge.subject,
        age=challenge.age,
        players=len(challenge.players),
        created_at=challenge.created_at,
        expires_at=challenge.expires_at,
    )
    if not review:
        return ChallengeOut(**base, content=ch.public_content(challenge))
    models = {"mcq": McqContent, "heads_up": HeadsUpContent, "diagram": DiagramContent}
    return ChallengeReview(**base, content=models[challenge.game](**challenge.content))


def readable(store: MemoryStore, challenge_id: str) -> StoredChallenge:
    found = store.get_challenge(player_id(), challenge_id)
    if found is None:
        raise HTTPException(404, "Challenge not found, expired, or unavailable to this player.")
    return found


NewBody = Annotated[
    NewChallenge,
    Body(
        openapi_examples={
            "mcq": {
                "summary": "Timed multiple choice on a topic",
                "value": {"game": "mcq", "topic": "Photosynthesis", "age": 12, "seconds": 30},
            },
            "heads_up": {
                "summary": "A heads-up deck for a room",
                "value": {
                    "game": "heads_up",
                    "topic": "The water cycle",
                    "age": 12,
                    "room_id": "<room id>",
                },
            },
            "diagram": {
                "summary": "A fill-in-the-blanks diagram",
                "value": {"game": "diagram", "topic": "How a volcano erupts", "age": 14},
            },
        }
    ),
]


@router.get(
    "/shared-topics", summary="Topics everyone in the room has studied", responses=errors(404)
)
def get_shared_topics(room_id: str, request: Request) -> SharedTopics:
    """Compare the memory of everyone in a room you belong to and return only the topics all of them have studied
    (a chat counts once it has had an answer). Topics are matched by meaning of their words, so "Photosynthesis"
    and "Plant photosynthesis" are the same. Nobody sees anything about topics only they studied, and nothing from
    anyone's chats. Use one as the `topic` of a challenge."""
    store = memory_of(request)
    members = room_members(room_id)
    ids = [m["user_id"] for m in members]
    people = []
    for m in members:
        profile = store.get_profile(m["user_id"])
        people.append(
            SharedPlayer(
                player_id=m["user_id"],
                display_name=m["display_name"],
                role=profile.role if profile else None,
            )
        )
    return SharedTopics(room_id=room_id, players=people, topics=ch.shared_topics(store, ids))


@router.post(
    "",
    status_code=201,
    summary="Make a challenge for a topic",
    responses=errors(404, 422, 502),
    dependencies=[Depends(generation_permit, scope="function")],
)
def create_challenge(body: NewBody, request: Request) -> ChallengeOut:
    """Have the AI write a game for a topic and freeze it, so everyone in the room gets exactly the same.

    - `mcq`: fixed multiple-choice questions for a timer per question.
    - `heads_up`: a deck of terms, each with 3 to 5 forbidden words, for the phone-on-the-forehead game.
    - `diagram`: a flowchart with some boxes blanked out and a word bank (with distractors) to drag back in.

    The response is the **public view**: no answers, and heads-up shows only the deck size, because the terms are
    secret from the player who guesses. The creator reads the full content from `/review`. The topic is screened
    and the content checked for the age you give, which should be the lowest among the players. Every challenge is
    casual: AI-written, not reviewed, and labelled so. It lasts 24 hours. Takes about 10-40 s."""
    store, creator = memory_of(request), player_id()
    members = room_members(body.room_id) if body.room_id else []
    store.purge_expired_challenges()
    try:
        made = ch.create_challenge(
            store,
            creator=creator,
            game=body.game,
            topic=body.topic,
            age=body.age,
            players=[m["user_id"] for m in members],
            room_id=body.room_id,
            subject=body.subject,
            count=body.count,
            seconds=body.seconds,
        )
    except (AgeBlocked, SafetyUnavailable):
        raise
    except ChallengeFailed as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        log.warning("challenge generation failed")
        raise HTTPException(502, "The challenge couldn't be written. Try again later.") from exc
    return out(made)


@router.get("/{challenge_id}", summary="Open a challenge", responses=errors(404))
def get_challenge(challenge_id: ChallengeId, request: Request) -> ChallengeOut:
    """The challenge without its answers. The creator and the members of its room can read it; anyone else, and an
    expired one, is a 404."""
    return out(readable(memory_of(request), challenge_id))


@router.get(
    "/{challenge_id}/review", summary="Read a challenge with its answers", responses=errors(404)
)
def review_challenge(challenge_id: ChallengeId, request: Request) -> ChallengeReview:
    """The full content: answers, explanations, the whole heads-up deck and the diagram's solution. Only the creator
    can read it; the other players get a 404 so the game cannot be spoiled."""
    found = readable(memory_of(request), challenge_id)
    if found.creator_id != player_id():
        raise HTTPException(404, "Challenge not found, expired, or unavailable to this player.")
    return out(found, review=True)


@router.delete(
    "/{challenge_id}", status_code=204, summary="Forget a challenge", responses=errors(404)
)
def delete_challenge(challenge_id: ChallengeId, request: Request) -> None:
    """Delete a challenge you created."""
    if not memory_of(request).delete_challenge(player_id(), challenge_id):
        raise HTTPException(404, "Challenge not found, expired, or unavailable to this player.")
