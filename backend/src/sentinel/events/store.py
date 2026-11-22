"""Write ``SentinelEvent``s exactly once.

Every producer gives a ``dedupe_key`` derived from the source (a webhook's
delivery ID, an idempotency key, the sample that decided a state change), so a
retried delivery or a repeated pass never stores an event twice.
"""

import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.db.models import Event, EventType

SEVERITIES = ("info", "warning", "critical")


async def record_event(
    db: AsyncSession,
    *,
    type: EventType,
    occurred_at: datetime,
    dedupe_key: str,
    payload: dict[str, Any],
    source_integration_id: uuid.UUID | None = None,
    entity_ids: Iterable[uuid.UUID] = (),
    severity: str | None = None,
) -> tuple[uuid.UUID, bool]:
    """Store an event. Returns (id, created); ``created`` is False for a duplicate."""
    if severity is not None and severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {SEVERITIES}")
    new_id = uuid.uuid4()
    stmt = (
        insert(Event)
        .values(
            id=new_id,
            type=type,
            source_integration_id=source_integration_id,
            entity_ids=sorted(set(entity_ids)),
            occurred_at=occurred_at,
            severity=severity,
            dedupe_key=dedupe_key,
            payload=payload,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(Event.id)
    )
    inserted = await db.scalar(stmt)
    if inserted is not None:
        return inserted, True
    existing = await db.scalar(select(Event.id).where(Event.dedupe_key == dedupe_key))
    if existing is None:  # pragma: no cover - deleted between the two statements
        raise RuntimeError("event vanished during de-duplication")
    return existing, False
