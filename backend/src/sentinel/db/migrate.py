"""Programmatic Alembic entry points (used by the CLI, `make migrate` and tests).

The database URL is passed through ``Config.attributes`` rather than the ini
file, so passwords never touch configparser interpolation or disk.
"""

import asyncio
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from sentinel.config import Settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["url"] = url
    return cfg


def _url(settings: Settings, url: str | None) -> str:
    return url or settings.effective_migration_url.get_secret_value()


async def _ping(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


def wait_for_database(
    url: str,
    *,
    timeout_s: float = 60.0,
    ping: Callable[[str], Coroutine[Any, Any, None]] = _ping,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wait until the database accepts connections.

    Containers start together: the database name may not resolve yet, or the
    server may not be listening (seen in CI as "Temporary failure in name
    resolution"). Only connection failures are retried; anything else, and the
    last failure after ``timeout_s``, is raised.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            asyncio.run(ping(url))
            return
        except (OSError, OperationalError):
            if time.monotonic() >= deadline:
                raise
            sleep(1.0)


def retry_connection(
    action: Callable[[], None],
    *,
    attempts: int = 5,
    delay_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Run ``action``, retrying a failure to connect (a name lookup can still flake
    after :func:`wait_for_database` succeeded; seen in CI). Postgres DDL is
    transactional, so a migration that failed part way left nothing behind."""
    for attempt in range(1, attempts + 1):
        try:
            action()
            return
        except (OSError, OperationalError):
            if attempt == attempts:
                raise
            sleep(delay_s)


def upgrade(settings: Settings, revision: str = "head", *, url: str | None = None) -> None:
    target = _url(settings, url)
    wait_for_database(target)
    retry_connection(lambda: command.upgrade(alembic_config(target), revision))


def downgrade(settings: Settings, revision: str, *, url: str | None = None) -> None:
    command.downgrade(alembic_config(_url(settings, url)), revision)


def current(settings: Settings, *, url: str | None = None) -> None:
    command.current(alembic_config(_url(settings, url)), verbose=True)


def revision(settings: Settings, message: str, *, url: str | None = None) -> None:
    """Autogenerate a new migration by diffing the models against the database."""
    command.revision(alembic_config(_url(settings, url)), message=message, autogenerate=True)
