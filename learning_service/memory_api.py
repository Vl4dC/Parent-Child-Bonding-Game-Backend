"""
Saved chats: profile, conversations filed by subject, and a compact digest of what a player has studied.

  GET  /api/memory/profile                  who this player id is (role label and display name)
  PUT  /api/memory/profile                  set it
  POST /api/memory/conversations            start a chat that will be remembered
  GET  /api/memory/conversations            this player's chats, newest first, with a count per subject
  GET  /api/memory/conversations/{id}       one chat with every message, to show it and continue it
  PATCH /api/memory/conversations/{id}      rename it or move it to another subject
  DELETE /api/memory/conversations/{id}     forget it
  GET  /api/memory/digest                   subjects and topics studied, without chat text (for competitions later)

A chat is continued by sending its `conversation_id` to POST /api/chat or /api/chat/image (chat.py, image_chat.py).
Everything is scoped to the player id (X-Player-ID); the stored role is a label, not a verified identity.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel, Field, StringConstraints, model_validator

from app.repository import current_session
from learning_service.api_docs import MEMORY, ConversationId, errors
from learning_service.memory import SUBJECTS, Subject
from learning_service.memory_store import (
    MAX_CONVERSATIONS,
    Conversation,
    Digest,
    MemoryFull,
    MemoryStore,
    Message,
    Role,
)
from learning_service.store import MATERIALS, get_or_404

router = APIRouter(prefix="/api/memory", tags=[MEMORY])

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]


class ProfileIn(BaseModel):
    role: Role = Field(
        ...,
        description="`parent` or `learner` (the child's label). A label, not proof of anything.",
    )
    display_name: Name = Field(
        ..., description="Shown in the app and, later, in friend competitions"
    )


class ProfileOut(BaseModel):
    player_id: str
    role: Role | None = Field(None, description="Null until a profile has been set")
    display_name: str | None = None


class NewConversation(BaseModel):
    material_id: str | None = Field(
        None, description="The upload (`POST /api/materials`) this chat is about, if any"
    )
    title: Title | None = Field(
        None, description="Leave out: the chat is named after its first answer"
    )


class ConversationPatch(BaseModel):
    title: Title | None = None
    subject: Subject | None = Field(None, description=f"One of: {', '.join(SUBJECTS)}")

    @model_validator(mode="after")
    def something_to_change(self):
        if self.title is None and self.subject is None:
            raise ValueError("Send a title, a subject or both")
        return self


class ConversationSummary(BaseModel):
    id: str
    title: str
    subject: str | None = Field(None, description="Null until the first answer has been written")
    topic: str | None = None
    material_name: str | None = Field(None, description="The PDF this chat was about")
    message_count: int
    created_at: str
    updated_at: str
    summarized_messages: int = Field(
        0, description="How many of the oldest messages are folded into the summary the model sees"
    )

    @classmethod
    def of(cls, c: Conversation) -> ConversationSummary:
        return cls(
            id=c.id,
            title=c.title,
            subject=c.subject,
            topic=c.topic,
            material_name=c.material_name,
            message_count=c.message_count,
            created_at=c.created_at,
            updated_at=c.updated_at,
            summarized_messages=c.summarized_upto,
        )


class ConversationList(BaseModel):
    items: list[ConversationSummary]
    subjects: dict[str, int] = Field(
        ..., description="How many chats this player has under each subject"
    )


class ConversationDetail(ConversationSummary):
    material_id: str | None = None
    summary: str = Field("", description="What the model is told about the older, folded messages")
    messages: list[Message] = Field(
        ..., description="The whole chat, oldest first. Folding never removes any"
    )


def memory_of(request: Request) -> MemoryStore:
    return request.app.state.memory


def player_id() -> str:
    return current_session.get().owner


def owned(store: MemoryStore, conversation_id: str) -> Conversation:
    conversation = store.get_conversation(player_id(), conversation_id)
    if conversation is None:
        raise HTTPException(404, "Conversation not found or unavailable to this player.")
    return conversation


ProfileBody = Annotated[
    ProfileIn,
    Body(
        openapi_examples={
            "learner": {"summary": "A child", "value": {"role": "learner", "display_name": "Sam"}},
            "parent": {"summary": "A parent", "value": {"role": "parent", "display_name": "Alex"}},
        }
    ),
]
NewBody = Annotated[
    NewConversation,
    Body(
        openapi_examples={
            "plain": {"summary": "A chat without a PDF", "value": {}},
            "pdf": {
                "summary": "A chat about my PDF (paste the material id)",
                "value": {"material_id": "<id from POST /api/materials>"},
            },
        }
    ),
]
PatchBody = Annotated[
    ConversationPatch,
    Body(
        openapi_examples={
            "move": {"summary": "Move it to another subject", "value": {"subject": "Science"}},
            "rename": {"summary": "Rename it", "value": {"title": "Photosynthesis homework"}},
        }
    ),
]


@router.get("/profile", summary="Who is this player?")
def get_profile(request: Request) -> ProfileOut:
    """The role label (`parent` or `learner`) and display name saved for this player id, or nulls if none was set.
    Different ids keep entirely separate memories, so a parent and a child each use their own id."""
    pid = player_id()
    profile = memory_of(request).get_profile(pid)
    return ProfileOut(
        player_id=pid,
        role=profile.role if profile else None,
        display_name=profile.display_name if profile else None,
    )


@router.put("/profile", summary="Set who this player is")
def put_profile(body: ProfileBody, request: Request) -> ProfileOut:
    """Save the role label and display name for this player id. The role is only a label for the app and for later
    features such as friend competitions: it is not a login and proves nothing about who is using the id."""
    pid = player_id()
    profile = memory_of(request).set_profile(pid, body.role, body.display_name)
    return ProfileOut(player_id=pid, role=profile.role, display_name=profile.display_name)


@router.post(
    "/conversations",
    status_code=201,
    summary="Start a chat that is remembered",
    responses=errors(404, 409),
)
def create_conversation(body: NewBody, request: Request) -> ConversationDetail:
    """Create an empty chat. Send its `id` as `conversation_id` to `POST /api/chat` (or `/api/chat/image`) and every
    question and answer is saved. After the first answer the chat is filed under a subject (Science, Math, ...)
    and named after its topic. With `material_id` the chat remembers which PDF it is about."""
    store = memory_of(request)
    name = None
    if body.material_id:
        name = get_or_404(MATERIALS, body.material_id, "Material").name
    try:
        created = store.create_conversation(
            player_id(),
            title=body.title or "New chat",
            material_id=body.material_id,
            material_name=name,
        )
    except MemoryFull as exc:
        raise HTTPException(409, str(exc)) from exc
    return ConversationDetail(**ConversationSummary.of(created).model_dump(), messages=[])


@router.get("/conversations", summary="My saved chats")
def list_conversations(
    request: Request,
    subject: Annotated[
        Subject | None, Query(description="Only chats filed under this subject")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_CONVERSATIONS)] = 100,
) -> ConversationList:
    """This player's chats, most recently used first, and how many there are per subject (for a sidebar).
    Chats that have not had an answer yet are counted under Other."""
    store, pid = memory_of(request), player_id()
    counts: dict[str, int] = {}
    for c in store.list_conversations(pid, limit=MAX_CONVERSATIONS):
        counts[c.subject or "Other"] = counts.get(c.subject or "Other", 0) + 1
    return ConversationList(
        items=[ConversationSummary.of(c) for c in store.list_conversations(pid, subject, limit)],
        subjects=counts,
    )


@router.get("/conversations/{conversation_id}", summary="Open a saved chat", responses=errors(404))
def get_conversation(conversation_id: ConversationId, request: Request) -> ConversationDetail:
    """The whole chat, oldest message first, with the summary the model is given for the folded older part. 404 for
    an unknown id or one that belongs to another player."""
    store = memory_of(request)
    conversation = owned(store, conversation_id)
    return ConversationDetail(
        **ConversationSummary.of(conversation).model_dump(),
        material_id=conversation.material_id,
        summary=conversation.summary,
        messages=store.messages(player_id(), conversation.id),
    )


@router.patch(
    "/conversations/{conversation_id}",
    summary="Rename a chat or move it to another subject",
    responses=errors(404),
)
def patch_conversation(
    conversation_id: ConversationId, body: PatchBody, request: Request
) -> ConversationSummary:
    """Change a chat's title or its subject. The model files each chat once; this is how a learner corrects it."""
    store = memory_of(request)
    owned(store, conversation_id)
    updated = store.update_conversation(
        player_id(), conversation_id, title=body.title, subject=body.subject
    )
    return ConversationSummary.of(updated)


@router.delete(
    "/conversations/{conversation_id}",
    status_code=204,
    summary="Forget a chat",
    responses=errors(404),
)
def delete_conversation(conversation_id: ConversationId, request: Request) -> None:
    """Delete the chat and everything saved in it. `DELETE /api/me/data` forgets all of a player's chats at once."""
    if not memory_of(request).delete_conversation(player_id(), conversation_id):
        raise HTTPException(404, "Conversation not found or unavailable to this player.")


@router.get("/digest", summary="What this player has studied")
def get_digest(request: Request) -> Digest:
    """A compact view for later features such as friend competitions: this player's subjects, and under each the
    topics studied with when. It holds no chat text and costs no model call."""
    return memory_of(request).digest(player_id())
