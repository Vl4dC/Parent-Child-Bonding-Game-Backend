"""Attempt budgets count failures too; no real model calls unless explicitly enabled."""

import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from datetime import datetime, timezone

from fastapi import HTTPException, Request

from app.repository import current_session

model_settings = ContextVar("model_settings", default=None)


class ContextPool(ThreadPoolExecutor):
    def submit(self, function, /, *args, **kwargs):
        return super().submit(copy_context().run, function, *args, **kwargs)


def reserve_request(repository, owner, limit):
    minute = int(time.time() // 60)
    if repository.durable:
        with repository.control() as connection:
            allowed = connection.execute(
                "select api_private.reserve_request(%s,%s,%s)", (owner, minute, limit)
            ).fetchone()[0]
    else:
        with repository.lock:
            key = (owner, "request_usage", str(minute))
            used = repository.data.get(key, 0)
            allowed = used < limit
            if allowed:
                repository.data[key] = used + 1
    if not allowed:
        raise HTTPException(429, "Request limit reached. Try again next minute.")


def reserve(repository, owner, limit):
    day = datetime.now(timezone.utc).date().isoformat()
    if repository.durable:
        with repository.control() as connection:
            row = connection.execute(
                "select api_private.reserve_generation(%s,%s,%s)", (owner, day, limit)
            ).fetchone()
            if not row[0]:
                raise HTTPException(429, "Daily generation attempt limit reached.")
    else:
        with repository.lock:
            key = (owner, "usage", day)
            used = repository.data.get(key, 0)
            if used >= limit:
                raise HTTPException(429, "Daily generation attempt limit reached.")
            repository.data[key] = used + 1


def generation_permit(request: Request):
    settings = request.app.state.settings
    if not settings.generation_enabled:
        raise HTTPException(503, "Model generation is disabled. Configure it before enabling.")
    if not request.app.state.generation_slots.acquire(blocking=False):
        raise HTTPException(429, "Generation is busy. Try again later.")
    try:
        unit = current_session.get()
        reserve(unit.repository, unit.owner, settings.generation_daily_limit)
        yield
    finally:
        request.app.state.generation_slots.release()
