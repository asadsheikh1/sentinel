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

    # Used from Step 2 onwards. Kept secret because the URL contains a password.
    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://sentinel:sentinel@localhost:5432/sentinel"
    )

    @property
    def is_prod(self) -> bool:
        return self.env is Environment.PROD


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings (cached; call ``cache_clear`` in tests)."""
    return Settings()
