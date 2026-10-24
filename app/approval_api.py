"""Additive approval routes. A registration receipt is never an application session."""

import asyncio
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator

from app.approval_service import ApprovalService
from app.models import User

router = APIRouter(
    tags=["Learner approval"],
    responses={
        401: {"description": "Verified session required"},
        403: {"description": "Email, request-specific authority, or proof rejected"},
        404: {"description": "Request not found or not authorized"},
        409: {"description": "Stale state or conflicting retry"},
        410: {"description": "Request receipt expired"},
        429: {"description": "Request or notification limit reached"},
        503: {"description": "Approval disabled or storage unavailable"},
    },
)
security = HTTPBearer(auto_error=False)
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]
CreationKey = Annotated[str, Header(alias="Idempotency-Key", min_length=32, max_length=128)]
Receipt = Annotated[
    SecretStr, Header(alias="X-Registration-Receipt", min_length=16, max_length=256)
]


class RegistrationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    learner_display_name: str = Field(alias="learnerDisplayName", min_length=1, max_length=60)
    guardian_email: EmailStr = Field(alias="parentEmail")
    learner_birth_year: int = Field(alias="learnerBirthYear", strict=True)

    @field_validator("learner_display_name")
    @classmethod
    def name(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("A nonempty single-line display name is required")
        return value.strip()

    @field_validator("learner_birth_year")
    @classmethod
    def year(cls, value):
        if not ApprovalService.birth_year_is_plausible(value):
            raise ValueError("Invalid birth year")
        return value


class RegistrationStatus(BaseModel):
    request_id: UUID
    status: Literal["pending", "approved", "declined", "expired"]
    version: int
    expires_at: int
    activated: Literal[False] = False


class RegistrationAccepted(RegistrationStatus):
    message: str
    receipt: str


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "decline"]
    expected_version: int = Field(ge=1)
    proof: SecretStr = Field(min_length=32, max_length=128)


class ApprovalMessage(BaseModel):
    message: str


class GuardianRequest(RegistrationStatus):
    learner_display_name: str
    learner_birth_year: int
    can_decide: bool


class GuardianPage(BaseModel):
    items: list[GuardianRequest]
    next_cursor: str | None


def service(request: Request):
    settings = request.app.state.settings
    if not settings.learner_approval_enabled:
        raise HTTPException(503, "Learner approval is not enabled.")
    repository = request.app.state.repository
    if repository is None:
        raise HTTPException(503, "Application storage is unavailable.")
    return ApprovalService(repository, settings.learner_approval_secret.get_secret_value())


async def public_service(request: Request):
    result = service(request)
    # Do not trust caller-controlled X-Forwarded-For. Configure trusted proxy handling separately.
    client = request.client.host if request.client else "unknown"
    request.state.approval_client = client
    await asyncio.to_thread(result.reserve, "public:" + client)
    return result


async def guardian(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
):
    result = service(request)
    client = request.client.host if request.client else "unknown"
    await asyncio.to_thread(result.reserve, "guardian-client:" + client)
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "Sign in to continue.")
    raw = await request.app.state.verify_identity(request, credentials.credentials)
    try:
        user = User.model_validate(raw)
    except ValueError:
        raise HTTPException(502, "Authentication returned an invalid identity.") from None
    if not user.email or not user.email_confirmed_at:
        raise HTTPException(403, "A verified guardian email is required.")
    await asyncio.to_thread(result.reserve, "guardian:" + str(user.id), 60)
    return result, str(user.id), user.email.casefold()


@router.post("/auth/registration/learners", response_model=RegistrationAccepted, status_code=202)
async def register(
    body: RegistrationInput,
    request: Request,
    key: CreationKey,
    approval: Annotated[ApprovalService, Depends(public_service)],
):
    payload = body.model_dump()
    payload["guardian_email"] = str(body.guardian_email).casefold()
    return await asyncio.to_thread(approval.create, payload, key, request.state.approval_client)


@router.get("/auth/registration/requests/{request_id}", response_model=RegistrationStatus)
async def status(
    request_id: UUID,
    receipt: Receipt,
    approval: Annotated[ApprovalService, Depends(public_service)],
):
    return await asyncio.to_thread(approval.status, str(request_id), receipt.get_secret_value())


@router.post(
    "/auth/registration/requests/{request_id}/resend",
    response_model=ApprovalMessage,
    status_code=202,
)
async def resend(
    request_id: UUID,
    receipt: Receipt,
    key: Key,
    approval: Annotated[ApprovalService, Depends(public_service)],
):
    return await asyncio.to_thread(
        approval.resend, str(request_id), receipt.get_secret_value(), key
    )


@router.get("/api/guardian/registration-requests", response_model=GuardianPage)
async def requests(
    identity: Annotated[tuple, Depends(guardian)],
    after: str = Query("", max_length=36),
    limit: int = Query(20, ge=1, le=50),
):
    approval, user_id, email = identity
    return await asyncio.to_thread(approval.list_requests, email, user_id, after, limit)


@router.post(
    "/api/guardian/registration-requests/{request_id}/decision",
    response_model=RegistrationStatus,
)
async def decision(
    request_id: UUID,
    body: DecisionInput,
    key: Key,
    identity: Annotated[tuple, Depends(guardian)],
):
    approval, user_id, email = identity
    payload = body.model_dump(exclude={"proof"}) | {"proof": body.proof.get_secret_value()}
    return await asyncio.to_thread(approval.decide, str(request_id), email, user_id, payload, key)


def install(app):
    app.include_router(router)
    # Extend CORS configuration before middleware construction; preserve existing origin/session
    # policy and existing headers. No cookies or wildcard origins are introduced.
    for middleware in app.user_middleware:
        if middleware.cls is CORSMiddleware:
            middleware.kwargs["allow_headers"] = [
                *middleware.kwargs["allow_headers"],
                "Idempotency-Key",
                "X-Registration-Receipt",
            ]
            middleware.kwargs["expose_headers"] = ["Retry-After", "X-Request-ID"]
