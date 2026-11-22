"""Telemetry gaps: a source stopped sending. Was it the agent, or the machine?

"Stale" alone can't tell an agent that was stopped from a host that died. A
check from the outside can (integration design, "stale is not host down"):

| Agent kind? | Probes of that machine | Classification | Severity |
|---|---|---|---|
| yes | all up | agent problem | info (no service incident) |
| yes | any down | host or network down | critical |
| yes | none / unknown | host or agent (unknown) | warning |
| no (an app, a webhook, ...) | — | source stopped sending | warning |

(A cloud connector's view of the instance joins this table with the AWS step.)

``gap_pass`` reconciles: a stale integration gets one firing ``telemetry_gap``
signal (re-classified every pass, as probes change), and the signal resolves
when data flows again or the integration is disabled.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sentinel.db.engine import session_scope
from sentinel.db.models import (
    CheckKind,
    Entity,
    EntityType,
    HealthCheck,
    Integration,
    IntegrationKind,
    IntegrationSignalStatus,
    IntegrationStatus,
    ProbeState,
    Signal,
    SignalKind,
    SignalStatus,
)

log = structlog.get_logger("sentinel.detection")
AGENT_KINDS = frozenset({IntegrationKind.LINUX_HOST, IntegrationKind.CONTAINER_HOST})


@dataclass(frozen=True)
class GapVerdict:
    classification: str
    severity: str
    summary: str


def classify(kind: IntegrationKind, probe_states: Iterable[ProbeState], name: str) -> GapVerdict:
    """The disambiguation table above. Pure."""
    states = [s for s in probe_states if s is not ProbeState.UNKNOWN]
    if kind not in AGENT_KINDS:
        return GapVerdict("source_stopped", "warning", f"{name} stopped sending telemetry")
    if any(s is ProbeState.DOWN for s in states):
        return GapVerdict(
            "host_or_network_down",
            "critical",
            f"{name}: no telemetry and its endpoint checks fail: host or network down",
        )
    if states and all(s is ProbeState.UP for s in states):
        return GapVerdict(
            "agent_problem",
            "info",
            f"{name}: no telemetry, but its endpoint checks pass: the agent has a problem",
        )
    return GapVerdict(
        "host_or_agent",
        "warning",
        f"{name}: no telemetry and nothing else to tell whether the host or the agent is down",
    )


def _check_host(check: HealthCheck) -> str:
    if check.kind is CheckKind.HTTP:
        return (urlsplit(check.target).hostname or "").lower()
    return check.target.rsplit(":", 1)[0].strip("[]").lower()


async def _machine_names(db: AsyncSession, integration_id: uuid.UUID) -> set[str]:
    """Host and container names this integration reported (what a probe would target)."""
    rows = await db.execute(
        select(Entity.type, Entity.key, Entity.display_name).where(
            Entity.integration_ids.contains([integration_id]),
            Entity.type.in_([EntityType.HOST, EntityType.CONTAINER]),
        )
    )
    names: set[str] = set()
    for kind, key, display in rows:
        name = (key if kind is EntityType.HOST else display).lower()
        names.add(name)
        names.add(name.split(".", 1)[0])
    return names


async def related_probe_states(db: AsyncSession, integration_id: uuid.UUID) -> list[ProbeState]:
    """States of enabled checks that target a machine this integration monitors."""
    names = await _machine_names(db, integration_id)
    if not names:
        return []
    checks = (await db.scalars(select(HealthCheck).where(HealthCheck.enabled.is_(True)))).all()
    related = []
    for check in checks:
        host = _check_host(check)
        if host in names or host.split(".", 1)[0] in names:
            related.append(check.state)
    return related


async def _last_seen(db: AsyncSession, integration_id: uuid.UUID) -> datetime | None:
    seen: datetime | None = await db.scalar(
        select(func.max(IntegrationSignalStatus.last_seen_at)).where(
            IntegrationSignalStatus.integration_id == integration_id
        )
    )
    return seen


REOPEN_WINDOW = timedelta(minutes=10)


async def _recently_resolved(
    db: AsyncSession, integration_id: uuid.UUID, now: datetime
) -> Signal | None:
    found: Signal | None = await db.scalar(
        select(Signal)
        .where(
            Signal.kind == SignalKind.TELEMETRY_GAP,
            Signal.source_integration_id == integration_id,
            Signal.status == SignalStatus.RESOLVED,
            Signal.ends_at >= now - REOPEN_WINDOW,
        )
        .order_by(Signal.ends_at.desc())
        .limit(1)
    )
    return found


async def gap_pass(
    session_factory: async_sessionmaker[AsyncSession], *, now: datetime | None = None
) -> list[tuple[str, str]]:
    """Open, re-classify and resolve telemetry-gap signals. Returns (integration, action)."""
    now = now or datetime.now(UTC)
    actions: list[tuple[str, str]] = []
    async with session_scope(session_factory) as db:
        integrations = (await db.scalars(select(Integration))).all()
        open_gaps = {
            s.source_integration_id: s
            for s in (
                await db.scalars(
                    select(Signal).where(
                        Signal.kind == SignalKind.TELEMETRY_GAP,
                        Signal.status == SignalStatus.FIRING,
                    )
                )
            ).all()
        }
        for integration in integrations:
            gap = open_gaps.get(integration.id)
            if integration.status is not IntegrationStatus.STALE:
                if gap is not None:
                    gap.status = SignalStatus.RESOLVED
                    gap.ends_at = now
                    gap.updated_at = now
                    gap.payload = {**gap.payload, "resolved_because": integration.status.value}
                    actions.append((integration.name, "resolved"))
                continue
            verdict = classify(
                integration.kind,
                await related_probe_states(db, integration.id),
                integration.name,
            )
            payload = {
                "classification": verdict.classification,
                "integration": integration.name,
                "integration_kind": integration.kind.value,
                "reason": integration.status_reason,
            }
            if gap is None:
                gap = await _recently_resolved(db, integration.id, now)
                if gap is not None:  # flapping: the same gap again, not a new signal
                    gap.status = SignalStatus.FIRING
                    gap.ends_at = None
                    gap.severity = verdict.severity
                    gap.title = verdict.summary
                    reopened = int(gap.payload.get("reopened", 0)) + 1
                    gap.payload = {**gap.payload, **payload, "reopened": reopened}
                    gap.updated_at = now
                    actions.append((integration.name, f"reopened:{verdict.classification}"))
                    continue
                last = await _last_seen(db, integration.id) or now
                db.add(
                    Signal(
                        kind=SignalKind.TELEMETRY_GAP,
                        status=SignalStatus.FIRING,
                        severity=verdict.severity,
                        title=verdict.summary,
                        source_integration_id=integration.id,
                        fingerprint=f"gap:{integration.id}",
                        labels={"sentinel_integration_id": str(integration.id)},
                        payload=payload,
                        starts_at=last,
                        dedupe_key=f"gap:{integration.id}:{int(last.timestamp())}",
                    )
                )
                actions.append((integration.name, f"opened:{verdict.classification}"))
            elif gap.payload.get("classification") != verdict.classification:
                gap.severity = verdict.severity
                gap.title = verdict.summary
                gap.payload = {**gap.payload, **payload}
                gap.updated_at = now
                actions.append((integration.name, f"reclassified:{verdict.classification}"))
    for name, action in actions:
        log.info("telemetry_gap", integration=name, action=action)
    return actions
