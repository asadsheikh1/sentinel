"""Session lifecycle: commit on success, rollback on error (no database needed)."""

from types import SimpleNamespace
from typing import Any

import pytest

from sentinel.api.deps import get_db_session
from sentinel.db.engine import session_scope


class FakeSession:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def __aenter__(self) -> "FakeSession":
        self.events.append("open")
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.events.append("close")

    async def commit(self) -> None:
        self.events.append("commit")

    async def rollback(self) -> None:
        self.events.append("rollback")


def _factory(session: FakeSession) -> Any:
    return lambda: session


async def test_commits_on_success() -> None:
    fake = FakeSession()
    async with session_scope(_factory(fake)) as s:
        assert s is fake
    assert fake.events == ["open", "commit", "close"]


async def test_rolls_back_and_reraises_on_error() -> None:
    fake = FakeSession()
    with pytest.raises(RuntimeError, match="boom"):
        async with session_scope(_factory(fake)):
            raise RuntimeError("boom")
    assert fake.events == ["open", "rollback", "close"]


async def test_dependency_closes_session_when_request_fails() -> None:
    """Regression: an error thrown into the FastAPI dependency must close the session."""
    fake = FakeSession()
    request: Any = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_factory=_factory(fake)))
    )
    gen = get_db_session(request)
    await anext(gen)
    with pytest.raises(RuntimeError, match="denied"):
        await gen.athrow(RuntimeError("denied"))
    assert fake.events == ["open", "rollback", "close"]


async def test_fastapi_dependency_uses_app_session_factory() -> None:
    fake = FakeSession()
    request: Any = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_factory=_factory(fake)))
    )
    async for s in get_db_session(request):
        assert s is fake
    assert fake.events == ["open", "commit", "close"]
