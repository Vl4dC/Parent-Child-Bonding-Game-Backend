from uuid import UUID

from pydantic import BaseModel, Field


class User(BaseModel):
    """Identity returned by the provider, used only to authorize application requests."""

    id: UUID
    email: str | None = None
    email_confirmed_at: str | None = None
    user_metadata: dict = Field(default_factory=dict)
