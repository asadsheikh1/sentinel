"""Migration lifecycle: upgrade → downgrade → upgrade, and models == migrations."""

import asyncio

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, inspect
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

import sentinel.db.models  # noqa: F401
from sentinel.config import Settings
from sentinel.db import migrate
from sentinel.db.base import Base

from .conftest import Pg

pytestmark = pytest.mark.db

EXPECTED_TABLES = {
    "users",
    "sessions",
    "api_keys",
    "audit_log",
    "jobs",
    "user_invites",
    "integrations",
    "ingest_tokens",
    "health_checks",
    "events",
    "signals",
    "deployments",
    "integration_signal_status",
    "entities",
    "entity_relations",
    "alembic_version",
}


async def _tables(url: str) -> set[str]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    finally:
        await engine.dispose()


async def test_upgrade_downgrade_upgrade_cycle(
    pg: Pg, fresh_database: str, base_settings: Settings
) -> None:
    url = pg.url("sentinel_migrator", fresh_database)
    # Alembic's env.py calls asyncio.run(), so it must run outside our event loop.
    await asyncio.to_thread(migrate.upgrade, base_settings, "head", url=url)
    assert await _tables(url) == EXPECTED_TABLES

    await asyncio.to_thread(migrate.downgrade, base_settings, "base", url=url)
    assert await _tables(url) == {"alembic_version"}

    await asyncio.to_thread(migrate.upgrade, base_settings, "head", url=url)
    assert await _tables(url) == EXPECTED_TABLES


async def test_models_match_migrations(migrator_engine: AsyncEngine) -> None:
    """Fails if someone changes a model without writing a migration (or vice versa)."""

    def diff(conn: Connection) -> list[object]:
        ctx = MigrationContext.configure(
            conn, opts={"compare_type": True, "compare_server_default": True}
        )
        return list(compare_metadata(ctx, Base.metadata))

    async with migrator_engine.connect() as conn:
        differences = await conn.run_sync(diff)
    assert differences == []


async def test_extensions_installed(migrator_engine: AsyncEngine) -> None:
    from sqlalchemy import text

    async with migrator_engine.connect() as conn:
        rows = await conn.execute(text("SELECT extname FROM pg_extension"))
        names = {r[0] for r in rows}
    assert {"citext", "pgcrypto", "vector"} <= names
