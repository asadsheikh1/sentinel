"""The live verification checklist shown while an integration is being set up.

Each item is one thing the user can watch tick (integrations design §5.2):
the token was used, each expected signal arrived, the installer's test marker
came through the log pipeline, the machine or service appeared in the
inventory, the agent runs the current configuration, the first probe ran.
Required items say whether setup worked; optional ones are extra information.

The two lookups that read the telemetry stores are passed in, so this stays
testable and the API can bound them with short timeouts. A store that can't be
reached leaves its item unticked with a note, never an error.
"""

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.db.models import (
    Entity,
    EntityType,
    HealthCheck,
    IngestToken,
    Integration,
    IntegrationKind,
    IntegrationSignalStatus,
)

log = structlog.get_logger("sentinel.integrations")

AGENT_KINDS = frozenset({IntegrationKind.LINUX_HOST, IntegrationKind.CONTAINER_HOST})
TOKEN_KINDS = AGENT_KINDS | {IntegrationKind.OTEL_APP}
SIGNAL_LABELS = {
    "metrics": "Metrics are arriving",
    "logs": "Logs are arriving",
    "traces": "Traces are arriving",
    "events": "Events are arriving",
}


def _signal_order(signal: str) -> int:
    return list(SIGNAL_LABELS).index(signal) if signal in SIGNAL_LABELS else len(SIGNAL_LABELS)


MarkerSeen = Callable[[uuid.UUID], Awaitable[bool]]  # the installer's marker is in Loki
ReportedHashes = Callable[[uuid.UUID], Awaitable[set[str]]]  # agent config hashes seen


@dataclass(frozen=True)
class CheckItem:
    key: str
    label: str
    done: bool
    required: bool = True
    detail: str | None = None
    at: datetime | None = None  # when it last happened, where that's known


def _names(entities: list[Entity], limit: int = 3) -> str:
    shown = ", ".join(e.display_name for e in entities[:limit])
    more = len(entities) - limit
    return shown + (f" and {more} more" if more > 0 else "")


async def _entities(db: AsyncSession, integration_id: uuid.UUID, kind: EntityType) -> list[Entity]:
    rows = await db.scalars(
        select(Entity)
        .where(Entity.type == kind, Entity.integration_ids.contains([integration_id]))
        .order_by(Entity.last_seen.desc())
        .limit(50)
    )
    return list(rows.all())


async def checklist(
    db: AsyncSession,
    integration: Integration,
    *,
    marker_seen: MarkerSeen | None = None,
    reported_hashes: ReportedHashes | None = None,
    expected_hash: str | None = None,
    now: datetime | None = None,
) -> list[CheckItem]:
    now = now or datetime.now(UTC)
    items: list[CheckItem] = []
    kind = integration.kind

    if kind in TOKEN_KINDS:
        used = await db.scalar(
            select(func.max(IngestToken.last_used_at)).where(
                IngestToken.integration_id == integration.id
            )
        )
        items.append(
            CheckItem(
                "token_used",
                "SENTINEL received data sent with the token",
                used is not None,
                at=used,
            )
        )
        seen = {
            s.signal: s
            for s in (
                await db.scalars(
                    select(IntegrationSignalStatus).where(
                        IntegrationSignalStatus.integration_id == integration.id
                    )
                )
            ).all()
        }
        for signal in sorted(integration.expected_signals, key=_signal_order):
            row = seen.get(signal)
            items.append(
                CheckItem(
                    f"signal:{signal}",
                    SIGNAL_LABELS.get(signal, f"{signal} arriving"),
                    row is not None,
                    at=row.last_seen_at if row else None,
                )
            )

    if kind in AGENT_KINDS:
        if integration.verified_at is None and marker_seen is not None:
            try:
                if await marker_seen(integration.id):
                    integration.verified_at = now
            except Exception as exc:  # the log store is down: just not ticked yet
                log.info("verification_marker_lookup_failed", error=type(exc).__name__)
        items.append(
            CheckItem(
                "marker",
                "The installer's test marker came through the log pipeline",
                integration.verified_at is not None,
                detail="proves logs flow end to end, from the machine's journal to SENTINEL",
            )
        )
        hosts = await _entities(db, integration.id, EntityType.HOST)
        items.append(
            CheckItem(
                "host", "The machine appears in the inventory", bool(hosts),
                detail=_names(hosts) if hosts else None,
            )
        )  # fmt: skip
        if kind is IntegrationKind.CONTAINER_HOST:
            containers = await _entities(db, integration.id, EntityType.CONTAINER)
            items.append(
                CheckItem(
                    "containers", "Containers appear in the inventory", bool(containers),
                    required=False, detail=_names(containers) if containers else None,
                )
            )  # fmt: skip
        if expected_hash and reported_hashes is not None:
            try:
                hashes = await reported_hashes(integration.id)
            except Exception:
                hashes = set()
            items.append(
                CheckItem(
                    "config",
                    "The agent runs the current configuration",
                    expected_hash in hashes,
                    required=False,
                    detail=(
                        None if expected_hash in hashes
                        else "not reported yet" if not hashes
                        else "out of date: re-run the installer to update it"
                    ),
                )
            )  # fmt: skip

    if kind is IntegrationKind.OTEL_APP:
        services = await _entities(db, integration.id, EntityType.SERVICE)
        items.append(
            CheckItem(
                "services", "The service appears in the inventory", bool(services),
                detail=_names(services) if services else None,
            )
        )  # fmt: skip

    if kind is IntegrationKind.HTTP_CHECK:
        checks = list(
            (
                await db.scalars(
                    select(HealthCheck).where(HealthCheck.integration_id == integration.id)
                )
            ).all()
        )
        items.append(
            CheckItem(
                "check", "A check is configured", bool(checks),
                detail=_names_of_checks(checks) if checks else None,
            )
        )  # fmt: skip
        ran = [c for c in checks if c.last_result_at is not None]
        items.append(
            CheckItem(
                "probe",
                "The first probe ran",
                bool(ran),
                detail=", ".join(f"{c.name}: {c.state.value}" for c in ran[:3]) if ran else None,
            )
        )
    return items


def _names_of_checks(checks: list[HealthCheck]) -> str:
    return ", ".join(f"{c.name} ({c.target})" for c in checks[:3])


# ------------------------------------------------------------------ the store lookups


def loki_marker(base_url: str, *, timeout_s: float = 3.0) -> MarkerSeen:
    """Did any ``sentinel-verify-`` line arrive from this integration in the last day?"""

    async def seen(integration_id: uuid.UUID) -> bool:
        params = {
            "query": f'{{sentinel_integration_id="{integration_id}"}} |= "sentinel-verify-"',
            "limit": "1",
            "start": str(int((time.time() - 86_400) * 1e9)),
        }
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout_s) as client:
            response = await client.get("/loki/api/v1/query_range", params=params)
            response.raise_for_status()
            return bool(response.json()["data"]["result"])

    return seen


def prometheus_hashes(base_url: str, *, timeout_s: float = 3.0) -> ReportedHashes:
    """The config hashes this integration's agents reported in the last 10 minutes."""

    async def hashes(integration_id: uuid.UUID) -> set[str]:
        query = (
            "group by (sentinel_agent_config_hash) (last_over_time("
            f'{{sentinel_integration_id="{integration_id}",sentinel_agent_config_hash!=""}}[10m]))'
        )
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout_s) as client:
            response = await client.get("/api/v1/query", params={"query": query})
            response.raise_for_status()
            rows = response.json()["data"]["result"]
        return {r["metric"]["sentinel_agent_config_hash"] for r in rows}

    return hashes
