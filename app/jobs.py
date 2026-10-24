"""Durable, explicitly executed generation jobs. Submission never calls a provider."""

import hashlib
import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import Field, ValidationError

from app.context import api_context
from app.platform import StrictModel, now
from app.repository import current_session
from learning_service import routes
from learning_service.lesson import LessonRequest

router = APIRouter(
    prefix="/api/jobs",
    tags=["Generation jobs"],
    dependencies=[Depends(api_context, scope="function")],
)


class CreateJob(StrictModel):
    kind: Literal["lesson", "quiz", "flashcards", "visuals", "followup"]
    resource_id: str | None = Field(None, max_length=128)
    parameters: dict = Field(default_factory=dict)


class JobResult(StrictModel):
    resource_id: str | None = None
    visual_ids: list[str] = Field(default_factory=list)


class Job(StrictModel):
    id: str
    kind: Literal["lesson", "quiz", "flashcards", "visuals", "followup"]
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    attempts: int
    created_at: datetime
    result: JobResult | None = None
    error: str | None = None
    lease_until: datetime | None = None
    source_id: str | None = None
    source_kind: str | None = None
    source_revision: int | None = None


MODELS = {
    "lesson": LessonRequest,
    "quiz": routes.CreateQuiz,
    "flashcards": routes.CreateDeck,
    "visuals": routes.VisualsIn,
    "followup": routes.FollowupIn,
}


def public(job):
    return {
        key: value for key, value in job.items() if key not in {"input", "digest", "lease_token"}
    }


@router.post("", status_code=202, response_model=Job)
def submit_job(
    body: CreateJob,
    request: Request,
    idempotency_key: str = Header(..., min_length=8, max_length=128),
):
    """Queue validated generation with an Idempotency-Key; submitting does not call a model."""
    if not request.app.state.settings.generation_enabled:
        raise HTTPException(503, "Model generation is disabled.")
    try:
        model = MODELS[body.kind]
        if set(body.parameters) - set(model.model_fields):
            raise ValueError("Unknown parameters")
        body.parameters = model.model_validate(body.parameters).model_dump(mode="json")
    except (ValidationError, ValueError):
        raise HTTPException(422, "Invalid generation parameters.") from None
    unit = current_session.get()
    if body.kind == "lesson" and body.resource_id is not None:
        raise HTTPException(422, "Lesson jobs use parameters.material_id, not resource_id.")
    if body.kind != "lesson":
        kind = "quizzes" if body.kind == "followup" else "lessons"
        if not body.resource_id or unit.get(kind, body.resource_id) is None:
            raise HTTPException(404, "Source resource not found.")
    if body.kind == "lesson" and body.parameters.get("material_id"):
        if unit.get("materials", body.parameters["material_id"]) is None:
            raise HTTPException(404, "Source material not found.")
    digest = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
    key = hashlib.sha256((unit.owner + idempotency_key).encode()).hexdigest()
    existing = unit.get("jobs", key)
    if existing:
        if existing["digest"] != digest:
            raise HTTPException(409, "Idempotency key already used for another request.")
        return public(existing)
    if unit.pending_jobs() >= 10:
        raise HTTPException(429, "At most ten pending jobs are allowed per account.")
    source_kind = "quizzes" if body.kind == "followup" else "lessons"
    source_id = body.resource_id
    if body.kind == "lesson":
        source_kind, source_id = "materials", body.parameters.get("material_id")
    source = unit.get(source_kind, source_id) if source_id else None
    job = {
        "id": key,
        "kind": body.kind,
        "input": body.model_dump(),
        "digest": digest,
        "status": "queued",
        "attempts": 0,
        "created_at": now(),
        "result": None,
        "error": None,
        "source_kind": source_kind if source_id else None,
        "source_id": source_id,
        "source_revision": source.get("revision", 1) if source else None,
    }
    unit.put("jobs", key, job)
    return public(job)


@router.get("/{job_id}", response_model=Job)
def get_job(job_id: str):
    """Return owned job status and result references without inputs or internal lease credentials."""
    job = current_session.get().get("jobs", job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")
    return public(job)


@router.delete("/{job_id}", status_code=204)
def cancel_job(job_id: str):
    """Cancel queued work. A running provider request cannot safely be cancelled by this API."""
    job = current_session.get().get("jobs", job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")
    if job["status"] == "cancelled":
        return
    if job["status"] != "queued":
        raise HTTPException(409, "Only queued work can be cancelled.")
    job["status"] = "cancelled"


def execute(job):
    if job.get("source_id"):
        source = current_session.get().get(job["source_kind"], job["source_id"])
        if source is None or source.get("revision", 1) != job["source_revision"]:
            raise HTTPException(409, "Generation source changed. Create a new job.")
    body = CreateJob.model_validate(job["input"])
    params = MODELS[body.kind].model_validate(body.parameters)
    handlers = {
        "quiz": routes.create_quiz,
        "flashcards": routes.create_deck,
        "visuals": routes.create_visuals,
        "followup": routes.create_followup,
    }
    result = (
        routes.create_lesson(params)
        if body.kind == "lesson"
        else handlers[body.kind](body.resource_id, params)
    )
    data = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    return {"resource_id": data.get("id"), "visual_ids": [v["id"] for v in data.get("visuals", [])]}
