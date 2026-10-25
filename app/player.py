"""Verified Clerk player IDs, with UUID selection only in explicit local test/development."""

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import Header, HTTPException, Request

DEFAULT_PLAYER_ID = UUID("00000000-0000-4000-8000-000000000001")


async def player_id(
    request: Request,
    value: Annotated[
        str | None,
        Header(
            alias="X-Player-ID",
            description="Optional player UUID for state selection, not authentication.",
        ),
    ] = None,
) -> str:
    settings = request.app.state.settings
    if settings.clerk_issuer:
        identity = await asyncio.to_thread(
            request.app.state.clerk_verifier.verify, request.headers.get("Authorization")
        )
        request.state.clerk_subject = identity["subject"]
        return identity["player_id"]
    if settings.app_env == "production":
        raise HTTPException(503, "Clerk authentication is not configured.")
    try:
        return str(UUID(value)) if value is not None else str(DEFAULT_PLAYER_ID)
    except ValueError:
        raise HTTPException(
            422, "X-Player-ID must be a UUID in explicit local test mode."
        ) from None
