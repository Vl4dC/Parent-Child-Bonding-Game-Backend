import asyncio
from contextlib import ExitStack
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.generation import model_settings, reserve_request
from app.models import User
from app.repository import current_session

security = HTTPBearer(auto_error=False)


async def api_context(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)]
):
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "Sign in to continue.", headers={"WWW-Authenticate": "Bearer"})
    user = await request.app.state.verify_identity(request, credentials.credentials)
    try:
        identity = User.model_validate(user)
        owner = str(UUID(str(identity.id)))
    except ValueError:
        raise HTTPException(502, "Authentication returned an invalid identity.") from None
    repository = request.app.state.repository
    if repository is None:
        raise HTTPException(503, "Application storage is not configured.")
    await asyncio.to_thread(
        reserve_request, repository, owner, request.app.state.settings.api_requests_per_minute
    )
    # Opening a DB connection and waiting for a lock must not block the event loop.
    stack = ExitStack()
    unit = await asyncio.to_thread(
        stack.enter_context, repository.session(owner, serialize=request.method != "GET")
    )
    token = current_session.set(unit)
    model_token = model_settings.set(request.app.state.settings)
    request.state.owner = owner
    try:
        yield unit
    except BaseException as error:
        await asyncio.to_thread(stack.__exit__, type(error), error, error.__traceback__)
        raise
    else:
        await asyncio.to_thread(stack.close)
    finally:
        model_settings.reset(model_token)
        current_session.reset(token)
