"""Probe state from the stored results: ``up``, ``down`` (2 failures in a row), ``unknown``.

Runs in the API every few seconds. Prometheus is the source of truth (the
prober only sends results), so a restart of either side loses nothing.

On a change, a ``probe_state`` ``SentinelEvent`` is stored. Its dedupe key is
derived from the deciding sample, so a repeated pass can't store it twice.
Each check's own (``http_check``) integration also counts as having sent
metrics when its results are fresh, which drives that integration's health.
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sentinel.db.engine import session_scope
from sentinel.db.models import Event, EventType, HealthCheck, ProbeState
from sentinel.integrations.entities import Query
from sentinel.integrations.gate import IngestGate
from sentinel.security.redaction import redact_text

log = structlog.get_logger("sentinel.checks")
WINDOW = "15m"
QUERY = f'probe_success{{sentinel_check_id!=""}}[{WINDOW}]'


@dataclass(frozen=True)
class Decision:
    state: ProbeState
    at: datetime | None  # time of the sample that decided it


def decide(
    current: ProbeState,
    samples: list[tuple[float, float]],
    *,
    interval_s: int,
    timeout_s: float,
    now: datetime,
) -> Decision:
    """Pure state rule. ``samples`` are (unix time, value), oldest first."""
    fresh_after = now.timestamp() - (3 * interval_s + timeout_s)
    recent = [(t, v) for t, v in samples if t >= fresh_after]
    if not recent:
        return Decision(ProbeState.UNKNOWN, None)
    last_t, last_v = recent[-1]
    at = datetime.fromtimestamp(last_t, UTC)
    if last_v >= 1:
        return Decision(ProbeState.UP, at)
    if len(recent) >= 2 and recent[-2][1] < 1:
        return Decision(ProbeState.DOWN, datetime.fromtimestamp(last_t, UTC))
    # A single failure: not enough to call it down (keeps flapping out).
    return Decision(current, at)


def series_by_check(rows: Iterable[Mapping[str, Any]]) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        check_id = row["metric"].get("sentinel_check_id")
        if not check_id:
            continue
        out[check_id].extend((float(t), float(v)) for t, v in row.get("values", []))
    for values in out.values():
        values.sort()
    return out


async def _record_event(
    db: AsyncSession, check: HealthCheck, previous: ProbeState, decision: Decision
) -> None:
    if decision.at is None:  # unknown states are not events
        return
    severity = "critical" if decision.state is ProbeState.DOWN else "info"
    stmt = insert(Event).values(
        id=uuid.uuid4(),
        type=EventType.PROBE_STATE,
        source_integration_id=check.integration_id,
        entity_ids=[check.entity_id] if check.entity_id else [],
        occurred_at=decision.at,
        severity=severity,
        dedupe_key=f"probe_state:{check.id}:{decision.state.value}:{int(decision.at.timestamp())}",
        payload={
            "check_id": str(check.id),
            "name": check.name,
            "kind": check.kind.value,
            "target": redact_text(check.target).text,
            "from": previous.value,
            "to": decision.state.value,
        },
    )
    await db.execute(stmt.on_conflict_do_nothing(index_elements=["dedupe_key"]))


async def probe_state_pass(
    session_factory: async_sessionmaker[AsyncSession],
    query: Query,
    gate: IngestGate | None = None,
    *,
    now: datetime | None = None,
) -> list[tuple[str, str]]:
    """One pass. Returns (check name, new state) for every change."""
    now = now or datetime.now(UTC)
    samples = series_by_check(await query(QUERY))
    changes: list[tuple[str, str]] = []
    async with session_scope(session_factory) as db:
        checks = (await db.scalars(select(HealthCheck).where(HealthCheck.enabled.is_(True)))).all()
        for check in checks:
            decision = decide(
                check.state,
                samples.get(str(check.id), []),
                interval_s=check.interval_s,
                timeout_s=check.timeout_s,
                now=now,
            )
            if decision.at is not None:
                check.last_result_at = decision.at
                if gate is not None and now - decision.at < timedelta(minutes=5):
                    gate.mark_seen(check.integration_id, "metrics", decision.at)
            if decision.state is check.state:
                continue
            previous = check.state
            check.state = decision.state
            check.state_changed_at = decision.at or now
            if decision.state is not ProbeState.UNKNOWN:
                await _record_event(db, check, previous, decision)
            changes.append((check.name, decision.state.value))
    for name, state in changes:
        log.info("probe_state_changed", check=name, state=state)
    return changes
