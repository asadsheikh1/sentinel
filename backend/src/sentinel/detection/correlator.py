"""The correlator: one problem → one incident, however many signals it raises.

Every ``correlation_interval_s`` it takes the signals not in an incident yet,
oldest first, and attaches each to an active incident (open, investigating or
mitigated) whose last signal was within ``correlation_window_s`` (15 min).
The first of these that holds decides:

1. ``same_entity``: the incident already involves the signal's entity.
2. ``same_service``: same service name (for a signal without an entity).
3. ``related_entity``: the entities are linked in the topology, in either
   direction, up to 3 hops along ``runs_on``, ``uses`` and ``fronts``. For
   example, a service and the host it runs on, or a service and one it calls.
   Two services that merely share a host are *not* related.
4. ``shared_trace``: the signal and the incident share a trace ID (error groups).
5. ``deploy_window``: the incident's suspected deployment shipped the signal's
   service too (same release), within the deployment lookback.

No match: a *firing* ``critical`` or ``warning`` signal opens a new incident.
An ``info`` signal (an agent problem with the host up) or one that has already
resolved never opens one; it can only join an incident that exists.

Afterwards, incidents whose signals have all resolved become ``mitigated``.
Like the error poller: one pass is one transaction, under an advisory lock.
"""

import uuid
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sentinel.config import Settings
from sentinel.db.engine import session_scope
from sentinel.db.models import (
    Deployment,
    EntityRelation,
    ErrorGroup,
    Incident,
    IncidentEntity,
    IncidentSignal,
    Signal,
    SignalKind,
    SignalStatus,
)
from sentinel.incidents import service
from sentinel.incidents.lifecycle import ACTIVE

log = structlog.get_logger("sentinel.detection")

LOCK_KEY = 0x5E7_12C0
RELATIONS = ("runs_on", "uses", "fronts")
MAX_HOPS = 3
LOOKBACK = timedelta(hours=6)  # unattached signals older than this are left alone
OPENING = frozenset({"critical", "warning"})

Graph = Mapping[uuid.UUID, frozenset[uuid.UUID]]


# ------------------------------------------------------------------ pure core


@dataclass(frozen=True)
class SignalView:
    id: uuid.UUID
    entity_id: uuid.UUID | None
    service_name: str | None
    starts_at: datetime
    trace_ids: frozenset[str] = frozenset()


@dataclass
class IncidentView:
    id: uuid.UUID
    last_signal_at: datetime
    entity_ids: set[uuid.UUID] = field(default_factory=set)
    services: set[str] = field(default_factory=set)
    trace_ids: set[str] = field(default_factory=set)
    release: set[str] = field(default_factory=set)  # services shipped with its deployment
    deployed_at: datetime | None = None

    def add(self, s: SignalView) -> None:
        if s.entity_id:
            self.entity_ids.add(s.entity_id)
        if s.service_name:
            self.services.add(s.service_name)
        self.trace_ids |= s.trace_ids
        self.last_signal_at = max(self.last_signal_at, s.starts_at)


def reachable(
    graph: Graph, start: uuid.UUID, targets: set[uuid.UUID], hops: int = MAX_HOPS
) -> bool:
    """Is any target reachable from ``start`` along directed edges in ``hops`` steps?"""
    seen = {start}
    queue = deque([(start, 0)])
    while queue:
        node, depth = queue.popleft()
        if depth == hops:
            continue
        for nxt in graph.get(node, frozenset()):
            if nxt in targets:
                return True
            if nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, depth + 1))
    return False


def related(graph: Graph, reverse: Graph, entity: uuid.UUID, others: set[uuid.UUID]) -> bool:
    """Linked in either direction: ``entity`` depends on one of ``others``, or one on it."""
    return bool(others) and (reachable(graph, entity, others) or reachable(reverse, entity, others))


def match(
    signal: SignalView,
    incidents: Iterable[IncidentView],
    graph: Graph,
    reverse: Graph,
    *,
    window: timedelta,
    deploy_lookback: timedelta,
) -> tuple[IncidentView, str] | None:
    """The incident this signal joins, and why; None to open a new one (pure)."""
    candidates = [i for i in incidents if i.last_signal_at >= signal.starts_at - window]
    tests = (
        ("same_entity", lambda i: signal.entity_id in i.entity_ids),
        ("same_service", lambda i: signal.entity_id is None and bool(signal.service_name)
         and signal.service_name in i.services),
        ("related_entity", lambda i: signal.entity_id is not None
         and related(graph, reverse, signal.entity_id, i.entity_ids)),
        ("shared_trace", lambda i: bool(signal.trace_ids & i.trace_ids)),
        ("deploy_window", lambda i: bool(signal.service_name) and signal.service_name in i.release
         and i.deployed_at is not None
         and i.deployed_at <= signal.starts_at <= i.deployed_at + deploy_lookback),
    )  # fmt: skip
    for reason, test in tests:
        hits = [i for i in candidates if test(i)]
        if hits:
            return max(hits, key=lambda i: i.last_signal_at), reason
    return None


# ------------------------------------------------------------------ database pass


@dataclass
class CorrelationResult:
    skipped: str | None = None
    opened: list[str] = field(default_factory=list)
    attached: list[tuple[str, str]] = field(default_factory=list)  # (incident, reason)
    left: int = 0  # unattached, can't open an incident
    mitigated: list[str] = field(default_factory=list)
    linked: list[str] = field(default_factory=list)  # a deployment found later


async def load_graph(db: AsyncSession) -> tuple[Graph, Graph]:
    forward: dict[uuid.UUID, set[uuid.UUID]] = {}
    backward: dict[uuid.UUID, set[uuid.UUID]] = {}
    rows = await db.execute(
        select(EntityRelation.from_entity_id, EntityRelation.to_entity_id).where(
            EntityRelation.relation.in_(RELATIONS)
        )
    )
    for src, dst in rows:
        forward.setdefault(src, set()).add(dst)
        backward.setdefault(dst, set()).add(src)
    return (
        {k: frozenset(v) for k, v in forward.items()},
        {k: frozenset(v) for k, v in backward.items()},
    )


async def _trace_ids(
    db: AsyncSession, signals: Iterable[Signal]
) -> dict[uuid.UUID, frozenset[str]]:
    by_group: dict[uuid.UUID, uuid.UUID] = {}
    for s in signals:
        if s.kind in (SignalKind.ERROR_GROUP_NEW, SignalKind.ERROR_GROUP_SPIKE):
            try:
                by_group[uuid.UUID(s.labels.get("error_group_id", ""))] = s.id
            except ValueError:
                continue
    if not by_group:
        return {}
    rows = await db.execute(
        select(ErrorGroup.id, ErrorGroup.sample_trace_ids).where(ErrorGroup.id.in_(list(by_group)))
    )
    return {by_group[gid]: frozenset(t for t in traces if t) for gid, traces in rows}


def _view(s: Signal, traces: Mapping[uuid.UUID, frozenset[str]]) -> SignalView:
    return SignalView(s.id, s.entity_id, s.service_name, s.starts_at, traces.get(s.id, frozenset()))


async def _incident_views(
    db: AsyncSession, since: datetime
) -> tuple[dict[uuid.UUID, Incident], dict[uuid.UUID, IncidentView]]:
    incidents = {
        i.id: i
        for i in (
            await db.scalars(
                select(Incident).where(
                    Incident.status.in_(ACTIVE), Incident.last_signal_at >= since
                )
            )
        ).all()
    }
    views = {iid: IncidentView(iid, i.last_signal_at) for iid, i in incidents.items()}
    if not incidents:
        return incidents, views
    for iid, eid in await db.execute(
        select(IncidentEntity.incident_id, IncidentEntity.entity_id).where(
            IncidentEntity.incident_id.in_(list(incidents))
        )
    ):
        views[iid].entity_ids.add(eid)
    attached = (
        await db.execute(
            select(IncidentSignal.incident_id, Signal)
            .join(Signal, Signal.id == IncidentSignal.signal_id)
            .where(IncidentSignal.incident_id.in_(list(incidents)))
        )
    ).all()
    traces = await _trace_ids(db, [s for _, s in attached])
    for iid, s in attached:
        views[iid].add(_view(s, traces))
    for iid, incident in incidents.items():
        if incident.suspected_deployment_id is None:
            continue
        deployment = await db.get(Deployment, incident.suspected_deployment_id)
        if deployment is not None:
            views[iid].deployed_at = deployment.deployed_at
            views[iid].release = await service.release_services(db, deployment)
    return incidents, views


async def correlate(db: AsyncSession, settings: Settings, *, now: datetime) -> CorrelationResult:
    result = CorrelationResult()
    window = timedelta(seconds=settings.correlation_window_s)
    deploy_lookback = timedelta(seconds=settings.deploy_lookback_s)
    pending = (
        await db.scalars(
            select(Signal)
            .outerjoin(IncidentSignal, IncidentSignal.signal_id == Signal.id)
            .where(IncidentSignal.signal_id.is_(None), Signal.received_at >= now - LOOKBACK)
            .order_by(Signal.starts_at, Signal.received_at)
        )
    ).all()
    if pending:
        graph, reverse = await load_graph(db)
        incidents, views = await _incident_views(db, now - LOOKBACK - window)
        traces = await _trace_ids(db, pending)
        leftovers: list[tuple[Signal, SignalView]] = []
        for signal in pending:
            view = _view(signal, traces)
            found = match(
                view, views.values(), graph, reverse, window=window, deploy_lookback=deploy_lookback
            )
            if found is not None:
                target, reason = found
                await service.attach(db, incidents[target.id], signal, reason, now=now)
                target.add(view)
                result.attached.append((incidents[target.id].ref, reason))
            elif signal.status is SignalStatus.FIRING and signal.severity in OPENING:
                incident = await service.open_incident(db, signal, settings=settings, now=now)
                incidents[incident.id] = incident
                fresh = IncidentView(incident.id, incident.last_signal_at)
                fresh.add(view)
                if incident.suspected_deployment_id is not None:
                    deployment = await db.get(Deployment, incident.suspected_deployment_id)
                    if deployment is not None:
                        fresh.deployed_at = deployment.deployed_at
                        fresh.release = await service.release_services(db, deployment)
                views[incident.id] = fresh
                result.opened.append(incident.ref)
            else:
                leftovers.append((signal, view))
        # A signal that can't open an incident may predate the one it belongs to,
        # opened later in this same pass: give it a second look.
        for signal, view in leftovers:
            found = match(
                view, views.values(), graph, reverse, window=window, deploy_lookback=deploy_lookback
            )
            if found is None:
                result.left += 1
                continue
            target, reason = found
            await service.attach(db, incidents[target.id], signal, reason, now=now)
            target.add(view)
            result.attached.append((incidents[target.id].ref, reason))
    result.linked = await service.link_late_deployments(db, settings=settings, now=now)
    quiet = timedelta(seconds=settings.mitigate_after_s)
    result.mitigated = await service.mitigate_quiet(db, quiet=quiet, now=now)
    return result


async def correlation_pass(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    now: datetime | None = None,
) -> CorrelationResult:
    now = now or datetime.now(UTC)
    async with session_scope(session_factory) as db:
        if not await db.scalar(select(func.pg_try_advisory_xact_lock(LOCK_KEY))):
            return CorrelationResult(skipped="another correlator holds the lock")
        result = await correlate(db, settings, now=now)
    if result.opened or result.attached or result.mitigated or result.linked:
        log.info(
            "correlation",
            opened=result.opened,
            attached=len(result.attached),
            mitigated=result.mitigated,
            linked=result.linked,
        )
    return result
