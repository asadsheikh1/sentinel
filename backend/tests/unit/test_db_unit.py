"""Database-layer behaviour that doesn't need a running Postgres."""

from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from sentinel.api.app import create_app
from sentinel.config import Environment, Settings
from sentinel.db import docs, migrate
from sentinel.db.base import Base

UNREACHABLE = "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none"


def test_readyz_returns_503_when_database_is_down() -> None:
    settings = Settings(env=Environment.TEST, database_url=SecretStr(UNREACHABLE))
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200  # liveness unaffected
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "database": "unavailable"}


def test_all_tables_registered() -> None:
    assert set(Base.metadata.tables) == {
        "users",
        "sessions",
        "api_keys",
        "audit_log",
        "jobs",
        "user_invites",
    }


def test_migration_url_falls_back_to_database_url() -> None:
    s = Settings(database_url=SecretStr(UNREACHABLE), migration_database_url=None)
    assert s.effective_migration_url.get_secret_value() == UNREACHABLE


def test_alembic_config_keeps_url_out_of_ini() -> None:
    cfg = migrate.alembic_config("postgresql+asyncpg://u:p%40ss@h/db")
    assert cfg.attributes["url"] == "postgresql+asyncpg://u:p%40ss@h/db"
    assert cfg.get_main_option("sqlalchemy.url") is None
    assert Path(str(cfg.get_main_option("script_location"))).is_dir()


def test_docs_render_contains_every_table_and_is_deterministic() -> None:
    first = docs.render()
    assert first == docs.render()
    for table in Base.metadata.tables:
        assert f"## `{table}`" in first
    assert "ix_jobs_claimable" in first
    assert "WHERE status = 'queued'" in first


def test_docs_check_detects_drift(tmp_path: Path) -> None:
    out = tmp_path / "database.md"
    assert docs.write_or_check(str(out), check=True) == 1  # missing
    assert docs.write_or_check(str(out), check=False) == 0
    assert docs.write_or_check(str(out), check=True) == 0
    out.write_text(out.read_text() + "edited by hand\n")
    assert docs.write_or_check(str(out), check=True) == 1


def test_committed_database_docs_are_up_to_date() -> None:
    committed = Path(__file__).resolve().parents[3] / "docs" / "database.md"
    assert committed.read_text() == docs.render(), "run `make db-docs`"
