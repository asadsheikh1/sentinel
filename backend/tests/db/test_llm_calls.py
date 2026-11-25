"""LLM call logging (Step 15) against real Postgres: llm_calls, payloads, retention, privileges."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from sentinel.config import Settings
from sentinel.db.engine import create_session_factory, session_scope
from sentinel.db.models import LLMCall, LLMCallPayload
from sentinel.llm import DbCallSink, LLMGateway, LLMSchemaError, prune_payloads
from sentinel.llm.fakes import FakeBackend

pytestmark = pytest.mark.db


class Answer(BaseModel):
    answer: str


def gateway(engine: AsyncEngine, script: dict[str, list[Any]], **settings: Any) -> LLMGateway:
    s = Settings(**settings)
    sink = DbCallSink(create_session_factory(engine), store_payloads=s.llm_store_payloads)
    return LLMGateway(FakeBackend(script), s, sink)


async def calls_for(engine: AsyncEngine, task: str) -> list[LLMCall]:
    async with session_scope(create_session_factory(engine)) as db:
        rows = await db.scalars(
            select(LLMCall).where(LLMCall.task == task).order_by(LLMCall.attempt)
        )
        return list(rows)


async def test_every_attempt_is_logged(app_engine: AsyncEngine) -> None:
    task = f"t-{uuid.uuid4().hex[:8]}"
    investigation = uuid.uuid4()
    llm = gateway(app_engine, {task: ["{}", {"answer": "42"}]})
    result = await llm.generate_structured(
        task, Answer, [{"role": "user", "content": "q"}], investigation_id=investigation,
        prompt_version="v3",
    )  # fmt: skip
    assert result.answer == "42"

    first, second = await calls_for(app_engine, task)
    assert (first.attempt, first.success, first.error_kind) == (1, False, "schema")
    assert (second.attempt, second.success, second.error_kind) == (2, True, None)
    assert first.request_id == second.request_id
    assert second.investigation_id == investigation
    assert second.prompt_version == "v3"
    assert second.operation == "chat"
    assert second.output_tokens is not None
    assert second.latency_ms >= 0
    assert len(second.prompt_sha256) == 64

    # Payloads are off by default: nothing stored.
    async with session_scope(create_session_factory(app_engine)) as db:
        stored = await db.get(LLMCallPayload, second.id)
    assert stored is None


async def test_failed_calls_are_logged_too(app_engine: AsyncEngine) -> None:
    task = f"t-{uuid.uuid4().hex[:8]}"
    llm = gateway(app_engine, {task: ["{}"]})
    with pytest.raises(LLMSchemaError):
        await llm.generate_structured(
            task, Answer, [{"role": "user", "content": "q"}], max_retries=0
        )
    (call,) = await calls_for(app_engine, task)
    assert not call.success
    assert call.error_kind == "schema"


async def test_payloads_when_enabled_and_their_retention(app_engine: AsyncEngine) -> None:
    task = f"t-{uuid.uuid4().hex[:8]}"
    llm = gateway(app_engine, {task: [{"answer": "yes"}]}, llm_store_payloads=True)
    await llm.generate_structured(task, Answer, [{"role": "user", "content": "the question"}])
    (call,) = await calls_for(app_engine, task)

    factory = create_session_factory(app_engine)
    async with session_scope(factory) as db:
        payload = await db.get(LLMCallPayload, call.id)
        assert payload is not None
        assert payload.request["messages"] == [{"role": "user", "content": "the question"}]
        assert payload.response == '{"answer": "yes"}'
        # Age it past the retention period.
        await db.execute(
            update(LLMCallPayload)
            .where(LLMCallPayload.llm_call_id == call.id)
            .values(created_at=datetime.now(UTC) - timedelta(days=31))
        )

    async with session_scope(factory) as db:
        assert await prune_payloads(db, retention_days=30) >= 1
    async with session_scope(factory) as db:
        assert await db.get(LLMCallPayload, call.id) is None
        assert await db.get(LLMCall, call.id) is not None  # the call itself is kept


async def test_a_logging_failure_does_not_break_the_call(app_engine: AsyncEngine) -> None:
    class Broken(DbCallSink):
        async def record(self, call: Any) -> None:
            call.error_kind = "not-a-kind"  # violates the check constraint
            await super().record(call)

    s = Settings()
    llm = LLMGateway(
        FakeBackend({"x": [{"answer": "ok"}]}), s, Broken(create_session_factory(app_engine))
    )
    assert (await llm.generate_structured("x", Answer, [{"role": "user", "content": "q"}])).answer


async def test_readonly_sees_calls_but_not_payloads(readonly_engine: AsyncEngine) -> None:
    async with readonly_engine.connect() as conn:
        await conn.execute(text("SELECT count(*) FROM llm_calls"))
        with pytest.raises(DBAPIError) as exc:
            await conn.execute(text("SELECT count(*) FROM llm_call_payloads"))
        assert "permission denied" in str(exc.value)
        await conn.rollback()
