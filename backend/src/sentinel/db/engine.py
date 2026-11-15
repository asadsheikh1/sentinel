"""Async engine and session management."""

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sentinel.config import Settings


def create_engine(settings: Settings, *, url: str | None = None) -> AsyncEngine:
    """Create the application engine.

    Every connection gets a ``statement_timeout`` so a runaway query can't hold
    a connection forever, and an ``application_name`` so it is identifiable in
    ``pg_stat_activity``.
    """
    return create_async_engine(
        url or settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        echo=settings.db_echo,
        connect_args={
            "server_settings": {
                "application_name": "sentinel",
                "statement_timeout": str(settings.db_statement_timeout_ms),
            }
        },
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: objects stay usable after commit (typical for async APIs).
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Yield a session; commit on success, roll back on error, always close."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def ping(engine: AsyncEngine) -> bool:
    """Return True if the database answers ``SELECT 1``."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        return False
    return True
