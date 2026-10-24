from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    app_env: Literal["development", "test", "production"] = "production"
    demo_mode: bool = False
    supabase_url: HttpUrl | None = None
    supabase_publishable_key: SecretStr | None = None
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    database_url: SecretStr | None = None
    database_pool_size: int = Field(8, ge=1, le=64)
    database_control_pool_size: int = Field(2, ge=1, le=16)
    database_pool_timeout: float = Field(5, ge=0.1, le=30)
    database_lock_timeout_ms: int = Field(3000, ge=100, le=30000)
    database_statement_timeout_ms: int = Field(30000, ge=1000, le=300000)
    generation_enabled: bool = False
    generation_concurrency: int = Field(2, ge=1, le=8)
    generation_daily_limit: int = Field(30, ge=1, le=1000)
    api_requests_per_minute: int = Field(120, ge=1, le=10000)
    job_attempt_limit: int = Field(2, ge=1, le=5)
    learner_approval_enabled: bool = False
    learner_approval_secret: SecretStr | None = None
    diagram_assembly_enabled: bool = False
    diagram_node_executable: str = "node"
    diagram_browser_executable: str | None = None
    diagram_browser_sandbox: bool = True
    diagram_render_timeout: int = Field(20, ge=5, le=60)
    diagram_render_concurrency: int = Field(1, ge=1, le=4)
    quiz_model: str = "anthropic:claude-sonnet-5-5"
    visual_model: str | None = None
    model_timeout_seconds: int = Field(60, ge=1, le=300)

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
        if self.learner_approval_enabled and (
            self.learner_approval_secret is None
            or len(self.learner_approval_secret.get_secret_value().encode()) < 32
        ):
            raise ValueError("Learner approval requires a private secret of at least 32 bytes")
        if self.demo_mode:
            if self.app_env not in ("development", "test"):
                raise ValueError("DEMO_MODE is allowed only with APP_ENV=development or test")
        elif self.supabase_url is None or self.supabase_publishable_key is None:
            raise ValueError("Supabase URL and publishable key are required when DEMO_MODE is off")
        return self
