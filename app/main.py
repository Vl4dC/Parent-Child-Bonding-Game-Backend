import asyncio
import secrets
import threading
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings
from app.demo import DemoAuth
from app.http_limits import BodyLimit
from app.models import (
    Credentials,
    EmailRequest,
    Message,
    PasswordRequest,
    RefreshRequest,
    Register,
    Session,
    User,
    VerifyRequest,
)
from app.repository import MemoryRepository, PostgresRepository

bearer = HTTPBearer(auto_error=False)
Bearer = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]


def create_app(
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    repository=None,
):
    settings = settings or Settings()
    owns_repository = repository is None
    if repository is None:
        if settings.database_url:
            repository = PostgresRepository.from_settings(settings)
        elif settings.app_env == "test" or settings.demo_mode:
            repository = MemoryRepository()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if repository is None:
            raise RuntimeError(
                "DATABASE_URL is required for application data outside test/demo mode"
            )
        if settings.app_env == "production" and not repository.durable:
            raise RuntimeError("Production requires PostgreSQL persistence")
        try:
            if settings.demo_mode:
                app.state.demo_auth = DemoAuth()
                yield
                return
            # No shared user sessions: each call explicitly supplies its own bearer token.
            async with httpx.AsyncClient(
                base_url=f"{str(settings.supabase_url).rstrip('/')}/auth/v1/",
                headers={"apikey": settings.supabase_publishable_key.get_secret_value()},
                timeout=15,
                transport=transport,
            ) as client:
                app.state.auth = client
                yield
        finally:
            if owns_repository and hasattr(repository, "close"):
                await asyncio.to_thread(repository.close)

    from learning_service.api_docs import DESCRIPTION, SWAGGER_UI, TAGS
    from learning_service.safety import AgeBlocked, SafetyUnavailable

    app = FastAPI(
        title="Family Learning API (LOCAL DEMO)" if settings.demo_mode else "Family Learning API",
        description=DESCRIPTION,
        openapi_tags=TAGS,
        swagger_ui_parameters=SWAGGER_UI,
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.repository = repository
    app.state.settings = settings
    app.state.generation_slots = threading.BoundedSemaphore(settings.generation_concurrency)
    app.add_middleware(BodyLimit)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # FastAPI's default validation response can echo passwords and refresh tokens.
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
                    for error in exc.errors()
                ]
            },
        )

    # Child-safety failures (learning_service/safety.py): the learner gets a fixed, friendly message,
    # never the unsafe text or anything the model provider said.
    @app.exception_handler(AgeBlocked)
    async def age_blocked(request: Request, exc: AgeBlocked):
        return JSONResponse(status_code=422, content={"detail": exc.message})

    @app.exception_handler(SafetyUnavailable)
    async def safety_unavailable(request: Request, exc: SafetyUnavailable):
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @app.exception_handler(psycopg.Error)
    async def storage_error(request: Request, exc):
        return JSONResponse(
            status_code=503, content={"detail": "Application storage is temporarily unavailable."}
        )

    @app.middleware("http")
    async def no_auth_cache(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Request-ID"] = secrets.token_hex(12)
        if request.url.path.startswith(("/auth/", "/api/")):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    async def upstream(
        request: Request,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        token: str | None = None,
        params: dict | None = None,
    ) -> dict:
        if settings.demo_mode:
            raise HTTPException(
                409, "This action is unavailable in demo mode. Use a seeded demo account."
            )
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            result = await request.app.state.auth.request(
                method,
                path,
                json=body,
                headers=headers,
                params=params,
            )
        except httpx.RequestError:
            raise HTTPException(
                503, "Authentication is temporarily unavailable. Try again."
            ) from None
        if result.is_error:
            try:
                code = result.json().get("error_code", "")
            except ValueError:
                code = ""
            # Duplicate registration and missing-account mail requests must not disclose identity.
            if path in ("signup", "recover", "resend") and code in (
                "user_already_exists",
                "email_exists",
                "user_not_found",
            ):
                return {}
            if result.status_code == 429:
                raise HTTPException(429, "Too many attempts. Please wait before trying again.")
            if code == "email_not_confirmed":
                raise HTTPException(403, "Confirm your email before signing in.")
            if result.status_code >= 500:
                raise HTTPException(503, "Authentication is temporarily unavailable. Try again.")
            if path == "user" and method == "PUT":
                raise HTTPException(
                    400, "Password update failed. Use a new password or sign in again."
                )
            if result.status_code in (401, 403) or path in ("token", "verify", "logout", "user"):
                raise HTTPException(401, "Invalid credentials, expired code, or expired session.")
            raise HTTPException(
                400, "Unable to complete this request. Check your details and try again."
            )
        if not result.content:
            return {}
        try:
            return result.json()
        except ValueError:
            raise HTTPException(502, "Unexpected authentication response.") from None

    async def require_token(credentials: Bearer) -> str:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(401, "Sign in to continue.", headers={"WWW-Authenticate": "Bearer"})
        if not settings.demo_mode and credentials.credentials.startswith("demo_"):
            raise HTTPException(401, "Demo sessions are not accepted in Supabase mode.")
        return credentials.credentials

    Token = Annotated[str, Depends(require_token)]

    @app.get("/health")
    async def health():
        return {"status": "ok", "auth_mode": "demo" if settings.demo_mode else "supabase"}

    @app.get("/ready")
    async def ready():
        try:
            ok = repository is not None and await asyncio.to_thread(repository.ready)
        except Exception:
            ok = False
        return JSONResponse(
            status_code=200 if ok else 503,
            content={"ready": ok, "durable": bool(repository and repository.durable)},
        )

    if settings.demo_mode:

        @app.get("/auth/demo-accounts", response_model=list[User])
        async def demo_accounts(request: Request):
            return request.app.state.demo_auth.accounts()

    @app.post("/auth/register", response_model=Message, status_code=202)
    async def register(data: Register, request: Request):
        # Metadata is a display preference, never a permission or verified guardian relationship.
        await upstream(
            request,
            "POST",
            "signup",
            body={
                "email": str(data.email),
                "password": data.password.get_secret_value(),
                "data": {
                    "display_name": data.display_name.strip(),
                    "account_type": data.account_type,
                },
            },
        )
        return {
            "message": "If registration is available for this address, check your email to confirm it."
        }

    @app.post("/auth/login", response_model=Session)
    async def login(data: Credentials, request: Request):
        if settings.demo_mode:
            return request.app.state.demo_auth.login(
                str(data.email), data.password.get_secret_value()
            )
        return await upstream(
            request,
            "POST",
            "token",
            params={"grant_type": "password"},
            body={
                "email": str(data.email),
                "password": data.password.get_secret_value(),
            },
        )

    @app.post("/auth/refresh", response_model=Session)
    async def refresh(data: RefreshRequest, request: Request):
        token = data.refresh_token.get_secret_value()
        if settings.demo_mode:
            return request.app.state.demo_auth.refresh(token)
        if token.startswith("demo_"):
            raise HTTPException(401, "Demo sessions are not accepted in Supabase mode.")
        return await upstream(
            request,
            "POST",
            "token",
            params={"grant_type": "refresh_token"},
            body={
                "refresh_token": data.refresh_token.get_secret_value(),
            },
        )

    @app.post("/auth/verify", response_model=Session)
    async def verify(data: VerifyRequest, request: Request):
        body = {"type": data.type}
        if data.token_hash is not None:
            body["token_hash"] = data.token_hash.get_secret_value()
        else:
            body["email"] = str(data.email)
            body["token"] = data.token.get_secret_value()
        return await upstream(request, "POST", "verify", body=body)

    @app.post("/auth/resend", response_model=Message, status_code=202)
    async def resend(data: EmailRequest, request: Request):
        await upstream(request, "POST", "resend", body={"email": str(data.email), "type": "signup"})
        return {"message": "If confirmation is needed, a new email will arrive shortly."}

    @app.get("/auth/me", response_model=User)
    async def me(request: Request, token: Token):
        if not settings.demo_mode and token.startswith("demo_"):
            raise HTTPException(401, "Demo sessions are not accepted in Supabase mode.")
        if settings.demo_mode:
            session = request.app.state.demo_auth.authenticate(token)
            return request.app.state.demo_auth.user(session.email)
        # Verify with Supabase; never trust a locally decoded, unverified JWT.
        return await upstream(request, "GET", "user", token=token)

    app.state.verify_identity = me

    @app.post("/auth/logout", status_code=204)
    async def logout(request: Request, token: Token):
        if settings.demo_mode:
            request.app.state.demo_auth.logout(token)
            return Response(status_code=204)
        await upstream(request, "POST", "logout", token=token, params={"scope": "local"})
        return Response(status_code=204)

    @app.post("/auth/forgot-password", response_model=Message, status_code=202)
    async def forgot_password(data: EmailRequest, request: Request):
        await upstream(request, "POST", "recover", body={"email": str(data.email)})
        return {"message": "If the account is eligible, a recovery email will arrive shortly."}

    @app.put("/auth/password", response_model=User)
    async def update_password(data: PasswordRequest, request: Request, token: Token):
        # Recovery sessions and ordinary authenticated sessions can update their own password.
        await upstream(request, "GET", "user", token=token)
        return await upstream(
            request,
            "PUT",
            "user",
            token=token,
            body={
                "password": data.password.get_secret_value(),
            },
        )

    from app.jobs import router as jobs_router
    from app.platform import router as platform_router
    from app.quiz_practice import install as install_quiz_practice
    from app.rooms import router as rooms_router
    from learning_service.routes import app as learning_router

    install_quiz_practice(app)

    common_errors = {
        401: {"description": "Authentication required"},
        429: {"description": "Request or generation limit reached"},
        503: {"description": "Storage or generation unavailable"},
    }
    app.include_router(learning_router, responses=common_errors)
    app.include_router(platform_router, responses=common_errors)
    app.include_router(jobs_router, responses=common_errors)
    app.include_router(rooms_router, responses=common_errors)
    from app.approval_api import install as install_approval

    install_approval(app)
    return app
