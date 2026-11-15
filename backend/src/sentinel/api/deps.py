"""FastAPI dependencies."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.db.engine import session_scope


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request: committed on success, rolled back on error."""
    async with session_scope(request.app.state.session_factory) as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db_session)]
