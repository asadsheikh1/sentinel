"""Application configuration.

All settings come from environment variables prefixed with ``SENTINEL_`` (or a
local ``.env`` file in development). Secrets use :class:`pydantic.SecretStr` so
they are never printed in logs or reprs.
"""

from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


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
