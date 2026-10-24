"""Unauthenticated player selection for data partitions and multiplayer state."""

from typing import Annotated
from uuid import UUID

from fastapi import Header

DEFAULT_PLAYER_ID = UUID("00000000-0000-4000-8000-000000000001")


def player_id(
    value: Annotated[
        UUID,
        Header(
            alias="X-Player-ID",
            description="Optional player UUID for state selection, not authentication.",
        ),
    ] = DEFAULT_PLAYER_ID,
) -> str:
    return str(value)
