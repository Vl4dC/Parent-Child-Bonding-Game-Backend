"""Owned resources, review boundaries, immutable revisions, and factual progress."""

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.context import api_context
from app.repository import current_session
from learning_service import store
from learning_service.lesson import Lesson
from learning_service.materials import Page

router = APIRouter(prefix="/api", dependencies=[Depends(api_context, scope="function")])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Preferences(StrictModel):
    theme: Literal["light", "dark"] = "light"
    text: Literal["md", "lg", "xl"] = "md"
    contrast: Literal["normal", "more"] = "normal"
    motion: Literal["full", "reduce"] = "full"
    captions: bool = True
    read_aloud: bool = False


class Profile(StrictModel):
    display_name: str = Field("Learner", min_length=1, max_length=60)
    daily_goal: int = Field(15, ge=1, le=120)
    avatar_reference: str | None = Field(None, max_length=128)
    preferences: Preferences = Field(default_factory=Preferences)


class Revision(StrictModel):
    expected_revision: int = Field(..., ge=1)


class PagesUpdate(Revision):
    pages: list[Page] = Field(..., min_length=1, max_length=600)


class LessonEdit(StrictModel):
    lesson: Lesson


class Approval(Revision):
    confirmed: Literal[True]


class ResourcePage(BaseModel):
    items: list[dict]
    next_cursor: str | None


class MaterialPages(BaseModel):
    id: str
    revision: int
    confirmed: bool
    pages: list[Page]


class MaterialStatus(BaseModel):
    revision: int
    confirmed: bool


class MaterialRevision(BaseModel):
    material_id: str
    revision: int
    name: str
    pages: list[Page]


class LessonRevision(BaseModel):
    id: str
    revision: int
    status: Literal["draft"]
    previous_id: str


class ApprovalRecord(BaseModel):
    reviewer_id: str
    reviewed_at: datetime
    kind: Literal["owner_confirmation"]


class LessonApproval(BaseModel):
    id: str
    status: Literal["approved"]
    approval: ApprovalRecord


class Completion(BaseModel):
    lesson_id: str
    completed_at: datetime


class Progress(BaseModel):
    lessons_completed: int
    quiz_answers: int
    correct_answers: int
    flashcard_reviews: int
    tournaments_submitted: int


def now():
    return datetime.now(timezone.utc).isoformat()


def all_records(unit, kind):
    cursor = ""
    while rows := unit.list(kind, after=cursor, limit=100):
        yield from rows
        cursor = rows[-1][0]


def delete_material_data(material_id):
    unit = current_session.get()
    lesson_ids = {
        key
        for key, data in all_records(unit, "lessons")
        if data.get("source") and data["source"].get("material_id") == material_id
    }
    derived = set(lesson_ids)
    question_sets = set()
    for kind in ("lessons", "quizzes", "tournaments", "decks", "completions", "material_revisions"):
        for key, data in all_records(unit, kind):
            if (
                key in lesson_ids
                or data.get("lesson_id") in lesson_ids
                or data.get("material_id") == material_id
            ):
                derived.add(key)
                if data.get("question_set_id"):
                    question_sets.add(data["question_set_id"])
                unit.delete(kind, key)
    for key in question_sets:
        unit.delete("question_sets", key)
    for key, data in all_records(unit, "jobs"):
        body = data["input"]
        if (
            body.get("resource_id") in derived
            or body.get("parameters", {}).get("material_id") == material_id
        ):
            unit.delete("jobs", key)
    material = unit.get("materials", material_id)  # a Material if the route already read it
    revisions = getattr(material, "revision", None) or (material or {}).get("revision", 0)
    for revision in range(1, revisions + 1):
        unit.delete("material_vectors", f"{material_id}:{revision}")
    unit.delete("materials", material_id)


def revision_matches(actual, expected):
    if actual != expected:
        raise HTTPException(409, "The resource changed. Read it again before updating.")


@router.get("/me/profile", response_model=Profile, tags=["Profile"])
def get_profile():
    return current_session.get().get("profile", "self") or Profile()


@router.put("/me/profile", response_model=Profile, tags=["Profile"])
def put_profile(body: Profile):
    if not body.display_name.strip():
        raise HTTPException(422, "Display name must contain text.")
    body.display_name = body.display_name.strip()
    current_session.get().put("profile", "self", body)
    return body


@router.delete("/me/data", status_code=204, tags=["Profile"])
def delete_own_data(request: Request):
    """Delete application data, remembered chats, the home, wallet and friends, and leave rooms."""
    unit = current_session.get()
    request.app.state.memory.forget_player(unit.owner)
    request.app.state.home.forget_player(unit.owner)
    for key, data in all_records(unit, "materials"):
        for revision in range(1, data["revision"] + 1):
            unit.delete("material_vectors", f"{key}:{revision}")
    if unit.connection:
        if unit.connection.execute(
            "select to_regprocedure('api_private.diagram_forget_player()') is not null"
        ).fetchone()[0]:
            unit.connection.execute(
                "select pg_advisory_xact_lock(hashtextextended('diagram-account-delete',0))"
            )
            unit.forget_diagram_player = True
    else:
        unit.forget_diagram_player = True
    for kind in (
        "profile",
        "materials",
        "material_revisions",
        "lessons",
        "quizzes",
        "question_sets",
        "tournaments",
        "decks",
        "completions",
        "economy_events",
        "jobs",
    ):
        for key, _ in all_records(unit, kind):
            unit.delete(kind, key)
    if unit.connection:
        unit.connection.execute("select api_private.leave_all_rooms()")
    else:
        from app.rooms import memory_command

        for room in list(unit.repository.rooms.values()):
            if any(m["user_id"] == unit.owner for m in room["members"]):
                memory_command(
                    unit.repository,
                    unit.owner,
                    "leave",
                    room["id"],
                    {"expected_version": room["version"]},
                )


@router.get("/materials/{material_id}/pages", tags=["Materials"], response_model=MaterialPages)
def material_pages(material_id: str):
    material = store.get_or_404(store.MATERIALS, material_id, "Material")
    return {
        "id": material.id,
        "revision": material.revision,
        "confirmed": material.confirmed,
        "pages": material.pages,
    }


@router.put("/materials/{material_id}/pages", tags=["Materials"], response_model=MaterialStatus)
def edit_pages(material_id: str, body: PagesUpdate):
    material = store.get_or_404(store.MATERIALS, material_id, "Material")
    revision_matches(material.revision, body.expected_revision)
    if [p.number for p in body.pages] != list(range(1, len(body.pages) + 1)):
        raise HTTPException(422, "Pages must be numbered consecutively from 1.")
    if sum(len(p.text) for p in body.pages) > 1_000_000:
        raise HTTPException(413, "Corrected text exceeds one million characters.")
    current_session.get().delete("material_vectors", f"{material_id}:{material.revision}")
    material.pages = body.pages
    material.revision += 1
    material.confirmed = False
    material.chapters = []  # old chapter boundaries no longer describe the corrected pages
    material.contents = []
    current_session.get().put(
        "material_revisions",
        f"{material_id}:{material.revision}",
        {
            "material_id": material_id,
            "revision": material.revision,
            "name": material.name,
            "pages": [page.model_dump(mode="json") for page in material.pages],
        },
    )
    return {"revision": material.revision, "confirmed": material.confirmed}


@router.get(
    "/materials/{material_id}/revisions/{revision}",
    tags=["Materials"],
    response_model=MaterialRevision,
)
def material_revision(material_id: str, revision: int):
    """Read the immutable extracted-text revision used by an older lesson's source reference."""
    store.get_or_404(store.MATERIALS, material_id, "Material")
    snapshot = current_session.get().get("material_revisions", f"{material_id}:{revision}")
    if snapshot is None:
        raise HTTPException(404, "Material revision not found.")
    return snapshot


@router.post("/materials/{material_id}/confirm", tags=["Materials"], response_model=MaterialStatus)
def confirm_material(material_id: str, body: Approval):
    material = store.get_or_404(store.MATERIALS, material_id, "Material")
    revision_matches(material.revision, body.expected_revision)
    if not any(p.text.strip() for p in material.pages):
        raise HTTPException(409, "There is no extracted text to confirm.")
    material.confirmed = True
    return {"revision": material.revision, "confirmed": True}


@router.post(
    "/lessons/{lesson_id}/revisions",
    status_code=201,
    tags=["Review"],
    response_model=LessonRevision,
)
def revise_lesson(lesson_id: str, body: LessonEdit):
    source = store.get_or_404(store.LESSONS, lesson_id, "Lesson")
    revised = source.model_copy(
        deep=True,
        update={
            "id": store.new_id(),
            "lesson": body.lesson,
            "revision": source.revision + 1,
            "status": "draft",
            "approval": None,
            "previous_id": source.id,
            "visuals": [],
            "auto_drawn": False,
        },
    )
    store.LESSONS[revised.id] = revised
    return {
        "id": revised.id,
        "revision": revised.revision,
        "status": revised.status,
        "previous_id": source.id,
    }


@router.post("/lessons/{lesson_id}/approve", tags=["Review"], response_model=LessonApproval)
def approve_lesson(lesson_id: str, body: Approval):
    lesson = store.get_or_404(store.LESSONS, lesson_id, "Lesson")
    revision_matches(lesson.revision, body.expected_revision)
    if not lesson.lesson.sections:
        raise HTTPException(409, "An empty lesson cannot be approved.")
    lesson.status = "approved"
    lesson.approval = lesson.approval or {
        "reviewer_id": current_session.get().owner,
        "reviewed_at": now(),
        "kind": "owner_confirmation",
    }
    return {"id": lesson.id, "status": lesson.status, "approval": lesson.approval}


@router.get("/tournament/{tournament_id}/review", tags=["Review"])
def review_tournament(tournament_id: str):
    quiz = store.get_or_404(store.TOURNAMENTS, tournament_id, "Tournament")
    # Explicit author-review API, separate from the answer-free play representation.
    return {
        "id": quiz.id,
        "questions": quiz.questions,
        "approved": quiz.approved,
        "review_kind": "owner_confirmation; not independent factual verification",
    }


@router.post("/tournament/{tournament_id}/approve", tags=["Review"])
def approve_tournament(tournament_id: str, body: Approval):
    quiz = store.get_or_404(store.TOURNAMENTS, tournament_id, "Tournament")
    revision_matches(1, body.expected_revision)
    quiz.approved = True
    quiz.approval = quiz.approval or {
        "reviewer_id": current_session.get().owner,
        "reviewed_at": now(),
        "kind": "owner_confirmation",
    }
    return {"id": quiz.id, "approved": True, "approval": quiz.approval}


@router.post("/quiz/{quiz_id}/attempts", status_code=201, tags=["Quiz"])
def new_attempt(quiz_id: str):
    template = store.get_or_404(store.QUIZZES, quiz_id, "Quiz")
    attempt = template.model_copy(
        deep=True,
        update={"id": store.new_id(), "attempts": {}, "submission": None, "parent_id": quiz_id},
    )
    store.QUIZZES[attempt.id] = attempt
    return attempt.public()


@router.post("/lessons/{lesson_id}/complete", tags=["Progress"], response_model=Completion)
def complete_lesson(lesson_id: str):
    lesson = store.get_or_404(store.LESSONS, lesson_id, "Lesson")
    unit = current_session.get()
    completion = unit.get("completions", lesson_id) or {
        "lesson_id": lesson_id,
        "completed_at": now(),
    }
    if unit.get("completions", lesson_id) is None:
        from app.economy import record

        record("lesson", lesson_id)
        if lesson.source:
            record("upload_learned", lesson.source.material_id)
    unit.put("completions", lesson_id, completion)
    return completion


@router.get("/me/progress", tags=["Progress"], response_model=Progress)
def progress():
    return current_session.get().progress()


@router.get("/resources/{kind}", response_model=ResourcePage, tags=["Resources"])
def resources(
    kind: Literal["materials", "lessons", "quizzes", "tournaments", "decks", "jobs", "completions"],
    after: str = "",
    limit: int = Query(50, ge=1, le=100),
):
    rows = current_session.get().summaries(kind, after=after, limit=limit + 1)
    # Summaries deliberately omit question keys, uploaded text, and job inputs.
    items = [
        {
            "id": key,
            **{
                k: data[k]
                for k in ("title", "name", "status", "revision", "lesson_id")
                if k in data
            },
        }
        for key, data in rows[:limit]
    ]
    return ResourcePage(items=items, next_cursor=rows[limit - 1][0] if len(rows) > limit else None)
