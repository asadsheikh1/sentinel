"""Where the gateway logs each call: ``llm_calls`` (always) and ``llm_call_payloads`` (opt-in).

Each call is written in its own short transaction, so a failed call is still
recorded when the caller's work rolls back. Logging must never break a call:
a failure to write is logged as a warning and swallowed.
"""

import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import structlog
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sentinel.db.engine import session_scope
from sentinel.db.ids import uuid7
from sentinel.db.models import LLMCall, LLMCallPayload

log = structlog.get_logger("sentinel.llm")

_MAX_ERROR_CHARS = 2000


@dataclass
class CallRecord:
    request_id: uuid.UUID
    attempt: int
    operation: str  # chat | embed
    task: str
    model: str
    prompt_version: str
    prompt_sha256: str
    thinking: bool
    latency_ms: int
    success: bool
    num_ctx: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    ttft_ms: int | None = None
    load_ms: int | None = None
    error_kind: str | None = None
    error: str | None = None
    investigation_id: uuid.UUID | None = None
    id: uuid.UUID = field(default_factory=uuid7)
    # Only stored when payload storage is on.
    request_payload: dict[str, Any] | None = None
    response: str | None = None
    thinking_text: str | None = None


class CallSink(Protocol):
    async def record(self, call: CallRecord) -> None: ...


class MemorySink:
    """Keeps records in a list (tests, the benchmark)."""

    def __init__(self) -> None:
        self.calls: list[CallRecord] = []

    async def record(self, call: CallRecord) -> None:
        self.calls.append(call)


class NullSink:
    async def record(self, call: CallRecord) -> None:
        return None


class DbCallSink:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, store_payloads: bool = False
    ) -> None:
        self.session_factory = session_factory
        self.store_payloads = store_payloads

    async def record(self, call: CallRecord) -> None:
        row = {
            k: v
            for k, v in asdict(call).items()
            if k not in ("request_payload", "response", "thinking_text")
        }
        if row["error"] is not None:
            row["error"] = row["error"][:_MAX_ERROR_CHARS]
        try:
            async with session_scope(self.session_factory) as db:
                db.add(LLMCall(**row))
                if self.store_payloads and call.request_payload is not None:
                    await db.flush()
                    db.add(
                        LLMCallPayload(
                            llm_call_id=call.id,
                            request=call.request_payload,
                            response=call.response,
                            thinking=call.thinking_text or None,
                        )
                    )
        except Exception as exc:  # logging must never break the call itself
            log.warning("llm_call_not_recorded", task=call.task, error=type(exc).__name__)


async def prune_payloads(
    db: AsyncSession, *, retention_days: int, now: datetime | None = None
) -> int:
    """Delete stored prompts and replies older than the retention period."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    result = await db.execute(delete(LLMCallPayload).where(LLMCallPayload.created_at < cutoff))
    count = int(getattr(result, "rowcount", 0) or 0)
    if count:
        log.info("llm_payloads_pruned", count=count)
    return count
