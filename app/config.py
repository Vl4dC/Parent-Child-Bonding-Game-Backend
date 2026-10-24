from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["development", "test", "production"] = "production"
    demo_mode: bool = False
    supabase_url: HttpUrl | None = None
    supabase_publishable_key: SecretStr | None = None
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    @field_validator("supabase_publishable_key")
    @classmethod
    def public_key_only(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().startswith("sb_publishable_"):
            raise ValueError("Use a Supabase publishable key, never a secret or service-role key")
        return value

    @field_validator("cors_origins")
    @classmethod
    def explicit_origins(cls, value: list[str]) -> list[str]:
        if "*" in value:
            raise ValueError("Use explicit CORS origins")
        return value

    @model_validator(mode="after")
    def validate_auth_mode(self):
        if self.demo_mode:
            if self.app_env not in ("development", "test"):
                raise ValueError("DEMO_MODE is allowed only with APP_ENV=development or test")
        elif self.supabase_url is None or self.supabase_publishable_key is None:
            raise ValueError("Supabase URL and publishable key are required when DEMO_MODE is off")
        return self
