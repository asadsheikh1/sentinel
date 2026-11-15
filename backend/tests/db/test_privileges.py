"""Role privileges and the append-only audit log, checked against real Postgres."""

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = pytest.mark.db

INSUFFICIENT_PRIVILEGE = "42501"
CHECK_VIOLATION = "23514"
UNIQUE_VIOLATION = "23505"

INSERT_AUDIT = text(
    "INSERT INTO audit_log (actor_type, actor_id, action, prev_hash, hash) "
    "VALUES ('system', NULL, 'test.event', :prev, :hash) RETURNING id"
)


def _sqlstate(exc: pytest.ExceptionInfo[DBAPIError]) -> Any:
    orig = exc.value.orig
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


async def _insert_audit(conn: AsyncConnection, n: int = 1) -> int:
    row = await conn.execute(INSERT_AUDIT, {"prev": "0" * 64, "hash": f"{n:064x}"})
    return int(row.scalar_one())


# ---------------------------------------------------------------- audit_log


async def test_app_can_insert_and_read_audit_log(app_conn: AsyncConnection) -> None:
    audit_id = await _insert_audit(app_conn)
    row = (
        await app_conn.execute(
            text("SELECT action FROM audit_log WHERE id = :id"), {"id": audit_id}
        )
    ).one()
    assert row.action == "test.event"


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_log SET action = 'tampered'",
        "DELETE FROM audit_log",
        "TRUNCATE audit_log",
    ],
)
async def test_app_cannot_modify_audit_log(app_engine: AsyncEngine, statement: str) -> None:
    async with app_engine.connect() as conn:
        await _insert_audit(conn, 2)
        with pytest.raises(DBAPIError) as exc:
            await conn.execute(text(statement))
        assert _sqlstate(exc) == INSUFFICIENT_PRIVILEGE
        # Blocked by the missing GRANT, before the trigger is even reached.
        assert "permission denied for table audit_log" in str(exc.value)
        await conn.rollback()


@pytest.mark.parametrize(
    "statement", ["UPDATE audit_log SET action = 'tampered'", "DELETE FROM audit_log"]
)
async def test_trigger_blocks_even_the_table_owner(
    migrator_engine: AsyncEngine, statement: str
) -> None:
    """Defence in depth: the owner has the grants, but the trigger still refuses."""
    async with migrator_engine.connect() as conn:
        await _insert_audit(conn, 3)
        with pytest.raises(DBAPIError) as exc:
            await conn.execute(text(statement))
        assert _sqlstate(exc) == INSUFFICIENT_PRIVILEGE
        assert "append-only" in str(exc.value)
        await conn.rollback()


# ------------------------------------------------------------- other roles


async def test_app_cannot_run_ddl(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as conn:
        with pytest.raises(DBAPIError) as exc:
            await conn.execute(text("CREATE TABLE evil (id int)"))
        assert _sqlstate(exc) == INSUFFICIENT_PRIVILEGE
        await conn.rollback()


async def test_readonly_can_read_users_but_not_secrets(readonly_engine: AsyncEngine) -> None:
    async with readonly_engine.connect() as conn:
        await conn.execute(text("SELECT count(*) FROM users"))
        for table in ("sessions", "api_keys"):
            with pytest.raises(DBAPIError) as exc:
                await conn.execute(text(f"SELECT * FROM {table}"))  # noqa: S608 - fixed names
            assert _sqlstate(exc) == INSUFFICIENT_PRIVILEGE
            await conn.rollback()


async def test_readonly_cannot_write(readonly_engine: AsyncEngine) -> None:
    async with readonly_engine.connect() as conn:
        with pytest.raises(DBAPIError) as exc:
            await conn.execute(
                text("INSERT INTO jobs (id, queue, kind) VALUES (gen_random_uuid(), 'q', 'k')")
            )
        assert _sqlstate(exc) == INSUFFICIENT_PRIVILEGE
        await conn.rollback()


# --------------------------------------------------------------------- jobs


async def test_job_defaults(app_conn: AsyncConnection) -> None:
    row = (
        await app_conn.execute(
            text(
                "INSERT INTO jobs (id, queue, kind) VALUES (gen_random_uuid(), 'investigations', "
                "'investigate') RETURNING status, attempts, max_attempts, priority, payload"
            )
        )
    ).one()
    assert row.status == "queued"
    assert (row.attempts, row.max_attempts, row.priority) == (0, 5, 0)
    assert row.payload == {}


async def test_job_status_check(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as conn:
        with pytest.raises(DBAPIError) as exc:
            await conn.execute(
                text(
                    "INSERT INTO jobs (id, queue, kind, status) "
                    "VALUES (gen_random_uuid(), 'q', 'k', 'bogus')"
                )
            )
        assert _sqlstate(exc) == CHECK_VIOLATION
        await conn.rollback()


async def test_job_dedupe_key_unique(app_engine: AsyncEngine) -> None:
    insert = text(
        "INSERT INTO jobs (id, queue, kind, dedupe_key) "
        "VALUES (gen_random_uuid(), 'q', 'k', 'same')"
    )
    async with app_engine.connect() as conn:
        await conn.execute(insert)
        with pytest.raises(DBAPIError) as exc:
            await conn.execute(insert)
        assert _sqlstate(exc) == UNIQUE_VIOLATION
        await conn.rollback()
