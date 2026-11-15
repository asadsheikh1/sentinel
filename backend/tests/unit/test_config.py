import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from sentinel.config import Environment, Settings, get_settings


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Make sure a developer's real .env or SENTINEL_* variables don't leak in.
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("SENTINEL_"):
            monkeypatch.delenv(key)
    get_settings.cache_clear()


def test_defaults() -> None:
    s = Settings()
    assert s.env is Environment.DEV
    assert s.api_port == 8000
    assert s.log_level == "INFO"


def test_env_vars_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTINEL_ENV", "prod")
    monkeypatch.setenv("SENTINEL_SECRET_KEY", "x" * 40)
    monkeypatch.setenv("SENTINEL_API_PORT", "9001")
    monkeypatch.setenv("SENTINEL_LOG_LEVEL", "DEBUG")
    s = Settings()
    assert s.is_prod
    assert s.api_port == 9001
    assert s.log_level == "DEBUG"


def test_dotenv_file_is_read(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SENTINEL_API_PORT=8123\n")
    assert Settings().api_port == 8123


def test_invalid_port_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTINEL_API_PORT", "70000")
    with pytest.raises(ValidationError):
        Settings()


def test_database_url_is_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTINEL_DATABASE_URL", "postgresql+asyncpg://u:hunter2@db/x")
    s = Settings()
    assert "hunter2" not in repr(s)
    assert "hunter2" not in str(s.model_dump())
    assert "hunter2" in s.database_url.get_secret_value()


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()
