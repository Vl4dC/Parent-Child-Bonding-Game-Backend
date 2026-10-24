from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, model_validator


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credentials(InputModel):
    email: EmailStr
    password: SecretStr = Field(min_length=1, max_length=128)


class Register(Credentials):
    password: SecretStr = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=60, pattern=r".*\S.*")
    account_type: Literal["learner", "parent"]


class EmailRequest(InputModel):
    email: EmailStr


class RefreshRequest(InputModel):
    refresh_token: SecretStr = Field(min_length=1, max_length=4096)


class VerifyRequest(InputModel):
    email: EmailStr | None = None
    token: SecretStr | None = Field(default=None, min_length=6, max_length=10)
    token_hash: SecretStr | None = Field(default=None, min_length=1, max_length=512)
    type: Literal["signup", "recovery"]

    @model_validator(mode="after")
    def one_verification_method(self):
        if self.token_hash is not None:
            if self.token is not None or self.email is not None:
                raise ValueError("Provide token_hash alone, or email and token together")
        elif self.email is None or self.token is None:
            raise ValueError("Provide token_hash alone, or email and token together")
        return self


class PasswordRequest(InputModel):
    password: SecretStr = Field(min_length=8, max_length=128)


class User(BaseModel):
    id: UUID
    email: str | None = None
    email_confirmed_at: str | None = None
    user_metadata: dict = Field(default_factory=dict)


class Session(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    expires_at: int | None = None
    user: User


class Message(BaseModel):
    message: str
