from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    app_env: Literal["development", "test", "production"] = "production"
    demo_mode: bool = False
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    database_url: SecretStr | None = None
    database_pool_size: int = Field(8, ge=1, le=64)
    database_control_pool_size: int = Field(2, ge=1, le=16)
    database_pool_timeout: float = Field(5, ge=0.1, le=30)
    database_lock_timeout_ms: int = Field(3000, ge=100, le=30000)
    database_statement_timeout_ms: int = Field(30000, ge=1000, le=300000)
    generation_enabled: bool = False
    # False lifts the daily attempt budget, the per-minute request budget and the busy gate.
    generation_limits_enabled: bool = True
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
    # Meaning-based book search. Empty disables it (keyword search only); needs the provider's API key.
    embedding_model: str | None = "openrouter:openai/text-embedding-3-small"
    embedding_dimensions: int = Field(512, ge=64, le=3072)
    illustrations_enabled: bool = False
    illustration_model: str = "anthropic/claude-haiku-5.5"
    illustrations_per_lesson: int = Field(2, ge=0, le=5)
    # SQLite file for chat memory (conversations kept per player). Unset: memory.db, or in memory for tests.
    memory_db_path: str | None = None
    # SQLite file for homes, wallets and friends. Unset: home.db, or in memory for tests.
    home_db_path: str | None = None
    # Only /api/social uses Clerk. Existing APIs keep their player-selector contract.
    clerk_issuer: str | None = None
    clerk_authorized_parties: list[str] = Field(default_factory=list)
    presence_ttl_seconds: int = Field(90, ge=30, le=300)
    game_invite_ttl_seconds: int = Field(600, ge=60, le=3600)
    game_invites_per_hour: int = Field(30, ge=1, le=100)
    model_timeout_seconds: int = Field(60, ge=1, le=300)

    @property
    def daily_limit(self) -> int | None:
        return self.generation_daily_limit if self.generation_limits_enabled else None

    @property
    def requests_per_minute(self) -> int | None:
        return self.api_requests_per_minute if self.generation_limits_enabled else None

    @field_validator("cors_origins")
    @classmethod
    def explicit_origins(cls, value: list[str]) -> list[str]:
        if "*" in value:
            raise ValueError("Use explicit CORS origins")
        return value

    @field_validator("clerk_issuer")
    @classmethod
    def trusted_issuer(cls, value):
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("CLERK_ISSUER must be an HTTPS origin without credentials or a path")
        return value.rstrip("/")

    @field_validator("clerk_authorized_parties")
    @classmethod
    def trusted_parties(cls, values):
        if any(not value or value == "*" for value in values):
            raise ValueError("Use exact authorized-party origins")
        return values

    @model_validator(mode="after")
    def validate_runtime(self):
        if self.learner_approval_enabled and (
            self.learner_approval_secret is None
            or len(self.learner_approval_secret.get_secret_value().encode()) < 32
        ):
            raise ValueError("Learner approval requires a private secret of at least 32 bytes")
        if self.demo_mode:
            if self.app_env not in ("development", "test"):
                raise ValueError("DEMO_MODE is allowed only with APP_ENV=development or test")
        return self
