"""Application configuration.

All settings come from environment variables prefixed with ``SENTINEL_`` (or a
local ``.env`` file in development). Secrets use :class:`pydantic.SecretStr` so
they are never printed in logs or reprs.
"""

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Environment(StrEnum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

# Insecure placeholder so dev/test work out of the box; rejected in prod.
_DEV_SECRET_KEY = "dev-only-insecure-secret-key-change-me-0000"  # noqa: S105
_DEV_INTERNAL_SECRET = "dev-only-internal-auth-secret-change-me-00"  # noqa: S105
_DEV_GITHUB_SECRET = "dev-only-github-webhook-secret"  # noqa: S105


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SENTINEL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Environment = Environment.DEV
    log_level: LogLevel = "INFO"
    log_json: bool = True

    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)

    # Database. URLs are secret because they contain passwords.
    # The app connects as `sentinel_app` (DML only, cannot UPDATE/DELETE audit_log).
    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://sentinel_app:sentinel-app-dev-only@localhost:5432/sentinel"
    )
    # Migrations connect as `sentinel_migrator` (owns the schema). Falls back to
    # `database_url` when unset (e.g. a throwaway test database).
    migration_database_url: SecretStr | None = SecretStr(
        "postgresql+asyncpg://sentinel_migrator:sentinel-migrator-dev-only@localhost:5432/sentinel"
    )
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)
    db_statement_timeout_ms: int = Field(default=30_000, ge=100)
    db_echo: bool = False

    # Security. `secret_key` is the server-side pepper for session tokens, API
    # keys, invite tokens and CSRF tokens. It MUST be set (>= 32 chars) in prod.
    secret_key: SecretStr = SecretStr(_DEV_SECRET_KEY)
    # False only for plain-http local dev (Safari drops Secure cookies on http://localhost).
    cookie_secure: bool = True
    session_idle_timeout_minutes: int = Field(default=12 * 60, ge=5)
    session_absolute_timeout_days: int = Field(default=7, ge=1, le=30)
    login_rate_limit_per_minute: int = Field(default=5, ge=1)
    lockout_threshold: int = Field(default=5, ge=1)
    lockout_minutes: int = Field(default=15, ge=1)
    invite_ttl_hours: int = Field(default=72, ge=1)
    password_min_length: int = Field(default=12, ge=8)

    # Ingestion gateway. Caddy proves itself to /internal/ingest-auth with this secret.
    internal_auth_secret: SecretStr = SecretStr(_DEV_INTERNAL_SECRET)
    ingest_default_rate_limit_per_min: int = Field(default=600, ge=1)
    ingest_auth_cache_ttl_s: float = Field(default=15.0, ge=0, le=30)

    # Integration health. Stale after 270 s + one 30 s evaluation = within 5 minutes.
    background_jobs_enabled: bool = True
    integration_flush_interval_s: float = Field(default=30.0, gt=0)
    integration_stale_after_s: float = Field(default=270.0, gt=0)
    integration_max_series: int = Field(default=5000, ge=1)
    entity_discovery_interval_s: float = Field(default=60.0, gt=0)
    prometheus_url: str = "http://127.0.0.1:9090"

    # SENTINEL Agent. The public ingest URL agents send to and download their
    # config and installer from (Caddy). `agent_dir` holds agent/ (templates,
    # installer); unset means the repository's agent/ directory.
    agent_public_endpoint: str = Field(
        default="http://127.0.0.1:4319", pattern=r"^https?://[A-Za-z0-9.:\[\]-]+(:[0-9]+)?$"
    )
    agent_dir: str | None = None

    # External health checks. The API provisions the prober's ingest token into
    # `prober_token_file` (a volume only the prober reads). The prober reaches
    # SENTINEL only through the ingest endpoint.
    prober_token_file: str | None = None
    prober_endpoint: str = "http://ingest.sentinel.internal:4319"
    prober_concurrency: int = Field(default=10, ge=1, le=50)
    prober_config_refresh_s: float = Field(default=30.0, gt=0)
    # Development only: exact host names the guard lets through although they
    # resolve to private addresses (the demo shop on this machine). Refused in prod.
    prober_allow_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    health_check_min_interval_s: int = Field(default=30, ge=5)
    health_check_max: int = Field(default=50, ge=1, le=1000)
    probe_state_interval_s: float = Field(default=15.0, gt=0)

    # Detection and change ingestion.
    # The API provisions Alertmanager's API key (scope alerts:write) into this file.
    alertmanager_key_file: str | None = None
    # Shared secret of the GitHub webhook (HMAC-SHA256). Unset: the endpoint is off.
    github_webhook_secret: SecretStr | None = None
    gap_interval_s: float = Field(default=30.0, gt=0)
    loki_url: str = "http://127.0.0.1:3100"
    jaeger_url: str = "http://127.0.0.1:16686"
    telemetry_timeout_s: float = Field(default=10.0, gt=0, le=60)
    telemetry_max_response_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)

    @field_validator("github_webhook_secret", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        return None if value in ("", None) else value

    @field_validator("prober_allow_hosts", mode="before")
    @classmethod
    def _comma_separated(cls, value: object) -> object:
        if isinstance(value, str):
            return [h.strip().lower() for h in value.split(",") if h.strip()]
        return value

    @model_validator(mode="after")
    def _prod_requires_real_secret(self) -> "Settings":
        if self.env is Environment.PROD:
            key = self.secret_key.get_secret_value()
            if key == _DEV_SECRET_KEY or len(key) < 32:
                raise ValueError("SENTINEL_SECRET_KEY must be set to >= 32 random chars in prod")
            internal = self.internal_auth_secret.get_secret_value()
            if internal == _DEV_INTERNAL_SECRET or len(internal) < 32:
                raise ValueError("SENTINEL_INTERNAL_AUTH_SECRET must be >= 32 random chars in prod")
            if not self.cookie_secure:
                raise ValueError("SENTINEL_COOKIE_SECURE must be true in prod")
            github = (
                self.github_webhook_secret.get_secret_value()
                if self.github_webhook_secret
                else None
            )
            if github is not None and (github == _DEV_GITHUB_SECRET or len(github) < 20):
                raise ValueError("SENTINEL_GITHUB_WEBHOOK_SECRET: >= 20 random chars in prod")
            if self.prober_allow_hosts:
                raise ValueError("SENTINEL_PROBER_ALLOW_HOSTS is for development only")
            if self.health_check_min_interval_s < 30:
                raise ValueError("SENTINEL_HEALTH_CHECK_MIN_INTERVAL_S must be >= 30 in prod")
        return self

    @property
    def effective_migration_url(self) -> SecretStr:
        return self.migration_database_url or self.database_url

    @property
    def is_prod(self) -> bool:
        return self.env is Environment.PROD


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings (cached; call ``cache_clear`` in tests)."""
    return Settings()
