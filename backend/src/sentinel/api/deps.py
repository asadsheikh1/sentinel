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


# scope="function": commit before the response is sent. FastAPI's default
# ("request") ends a yield dependency after the response, so a client acting on
# a response (a request right after login) could miss the write.
DbSession = Annotated[AsyncSession, Depends(get_db_session, scope="function")]
