import asyncio
import secrets
import threading
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import Settings
from app.http_limits import BodyLimit
from app.repository import MemoryRepository, PostgresRepository


def create_app(
    settings: Settings | None = None,
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
        allow_headers=["Content-Type", "X-Player-ID", "Authorization"],
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Keep submitted values and sensitive validation contexts out of errors.
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
    async def no_api_cache(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Request-ID"] = secrets.token_hex(12)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        try:
            ok = repository is not None and await asyncio.to_thread(repository.ready)
        except Exception:
            ok = False
        if not ok and settings.diagram_assembly_enabled:
            diagram = {
                "enabled": True,
                "ready": False,
                "state": "unavailable",
                "reason": "storage_unavailable",
                "schema_ready": False,
                "renderer_ready": False,
                "checked_at": None,
            }
        else:
            diagram = await asyncio.to_thread(
                app.state.diagram_preflight.check, repository, settings
            )
        ok = ok and diagram["ready"]
        return JSONResponse(
            status_code=200 if ok else 503,
            content={
                "ready": ok,
                "durable": bool(repository and repository.durable),
                "features": {"diagram_assembly": diagram},
            },
            headers={"Cache-Control": "no-store"},
        )

    from app.diagram_api import install as install_diagram_assembly
    from app.jobs import router as jobs_router
    from app.platform import router as platform_router
    from app.quiz_practice import install as install_quiz_practice
    from app.rooms import router as rooms_router
    from learning_service.routes import app as learning_router

    install_quiz_practice(app)
    install_diagram_assembly(app)

    common_errors = {
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
