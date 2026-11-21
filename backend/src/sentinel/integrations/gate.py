"""The ingest gate behind Caddy's ``forward_auth``: called on every OTLP request.

It must be cheap, so tokens are cached briefly and last-seen data is kept in
memory and flushed to Postgres periodically. A token revoked through the API is
dropped from the cache at once; the TTL bounds any other delay.
"""

import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sentinel.config import Settings
from sentinel.db.engine import session_scope
from sentinel.db.models import (
    ActorType,
    IngestToken,
    Integration,
    IntegrationSignalStatus,
    IntegrationStatus,
)
from sentinel.integrations.health import compute_health
from sentinel.integrations.tokens import hash_ingest_token
from sentinel.security import audit

log = structlog.get_logger("sentinel.integrations")

SIGNAL_BY_PATH = {"/v1/metrics": "metrics", "/v1/logs": "logs", "/v1/traces": "traces"}
_MAX_CACHE = 10_000
_NEGATIVE_TTL_S = 5.0


@dataclass(frozen=True)
class TokenInfo:
    token_id: uuid.UUID
    integration_id: uuid.UUID
    kind: str
    rate_limit_per_min: int


@dataclass(frozen=True)
class GateResult:
    status: int  # 200, 401 or 429
    info: TokenInfo | None = None
    retry_after_s: int | None = None


class PerMinuteLimiter:
    """Fixed one-minute window per token."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._windows: dict[uuid.UUID, tuple[int, int]] = {}
        self._lock = Lock()

    def hit(self, key: uuid.UUID, limit: int) -> int | None:
        """Record a request. Returns None if allowed, else seconds until the next window."""
        now = self._clock()
        minute = int(now // 60)
        with self._lock:
            window, count = self._windows.get(key, (minute, 0))
            if window != minute:
                count = 0
            if count >= limit:
                return max(1, 60 - int(now % 60))
            self._windows[key] = (minute, count + 1)
            return None


class IngestGate:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self._clock = clock
        self._secret = settings.secret_key.get_secret_value()
        self._cache: dict[str, tuple[float, TokenInfo | None]] = {}
        self.limiter = PerMinuteLimiter()
        self._lock = Lock()
        self._seen: dict[tuple[uuid.UUID, str], datetime] = {}
        self._counts: dict[tuple[uuid.UUID, str], int] = defaultdict(int)
        self._token_used: dict[uuid.UUID, datetime] = {}
        self.series_counts: dict[str, int] = {}

    # ------------------------------------------------------------- request path

    async def _lookup(self, token: str) -> TokenInfo | None:
        token_hash = hash_ingest_token(self._secret, token)
        cached = self._cache.get(token_hash)
        now = self._clock()
        if cached and cached[0] > now:
            return cached[1]
        async with self.session_factory() as db:
            row = (
                await db.execute(
                    select(IngestToken, Integration)
                    .join(Integration, IngestToken.integration_id == Integration.id)
                    .where(
                        IngestToken.token_hash == token_hash,
                        IngestToken.revoked_at.is_(None),
                        Integration.status.not_in(
                            [IntegrationStatus.DISABLED, IntegrationStatus.REVOKED]
                        ),
                    )
                )
            ).first()
        info = (
            TokenInfo(row[0].id, row[1].id, row[1].kind.value, row[0].rate_limit_per_min)
            if row
            else None
        )
        ttl = self.settings.ingest_auth_cache_ttl_s if info else _NEGATIVE_TTL_S
        if len(self._cache) >= _MAX_CACHE:
            self._cache.clear()
        self._cache[token_hash] = (now + ttl, info)
        return info

    async def authenticate(self, token: str) -> TokenInfo | None:
        """Resolve a token without counting it as telemetry (agent config downloads)."""
        return await self._lookup(token)

    async def check(self, token: str | None, signal: str | None) -> GateResult:
        if token is None or signal is None:
            return GateResult(401)
        info = await self._lookup(token)
        if info is None:
            return GateResult(401)
        retry = self.limiter.hit(info.token_id, info.rate_limit_per_min)
        if retry is not None:
            return GateResult(429, info, retry)
        self.record(info, signal)
        return GateResult(200, info)

    def record(self, info: TokenInfo, signal: str) -> None:
        now = datetime.now(UTC)
        with self._lock:
            self._seen[(info.integration_id, signal)] = now
            self._counts[(info.integration_id, signal)] += 1
            self._token_used[info.token_id] = now

    def invalidate(self, integration_id: uuid.UUID) -> None:
        """Forget cached tokens of an integration (after revoke, rotate or disable)."""
        for key, (_, info) in list(self._cache.items()):
            if info is not None and info.integration_id == integration_id:
                self._cache.pop(key, None)

    def clear_negative_cache(self) -> None:
        for key, (_, info) in list(self._cache.items()):
            if info is None:
                self._cache.pop(key, None)

    # ----------------------------------------------------------- background jobs

    async def flush(self) -> int:
        """Write in-memory last-seen data to Postgres. Returns rows written."""
        with self._lock:
            seen, self._seen = self._seen, {}
            counts, self._counts = self._counts, defaultdict(int)
            used, self._token_used = self._token_used, {}
        if not seen:
            return 0
        async with session_scope(self.session_factory) as db:
            known = set(
                await db.scalars(
                    select(Integration.id).where(Integration.id.in_({k[0] for k in seen}))
                )
            )
            for (integration_id, signal), at in seen.items():
                if integration_id not in known:  # deleted meanwhile
                    continue
                stmt = insert(IntegrationSignalStatus).values(
                    integration_id=integration_id,
                    signal=signal,
                    last_seen_at=at,
                    last_count_window=counts[(integration_id, signal)],
                )
                await db.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["integration_id", "signal"],
                        set_={
                            "last_seen_at": stmt.excluded.last_seen_at,
                            "last_count_window": stmt.excluded.last_count_window,
                        },
                    )
                )
            for token_id, at in used.items():
                token = await db.get(IngestToken, token_id)
                if token is not None:
                    token.last_used_at = at
        return len(seen)

    async def evaluate(self, *, now: datetime | None = None) -> list[tuple[str, str]]:
        """Recompute every integration's status. Returns (name, new status) for changes."""
        now = now or datetime.now(UTC)
        stale_after = timedelta(seconds=self.settings.integration_stale_after_s)
        changed: list[tuple[str, str]] = []
        async with session_scope(self.session_factory) as db:
            integrations = (
                await db.scalars(
                    select(Integration).where(
                        Integration.status.not_in(
                            [IntegrationStatus.DISABLED, IntegrationStatus.REVOKED]
                        )
                    )
                )
            ).all()
            rows = (await db.scalars(select(IntegrationSignalStatus))).all()
            last_seen: dict[uuid.UUID, dict[str, datetime]] = defaultdict(dict)
            for row in rows:
                last_seen[row.integration_id][row.signal] = row.last_seen_at
            for integration in integrations:
                health = compute_health(
                    integration.status,
                    integration.expected_signals,
                    last_seen[integration.id],
                    now=now,
                    stale_after=stale_after,
                    series_count=self.series_counts.get(str(integration.id), 0),
                    max_series=self.settings.integration_max_series,
                )
                if (health.status, health.reason) == (
                    integration.status,
                    integration.status_reason,
                ):
                    continue
                previous = integration.status
                integration.status = health.status
                integration.status_reason = health.reason
                if previous is not health.status:
                    changed.append((integration.name, health.status.value))
                    await audit.append(
                        db,
                        actor_type=ActorType.SYSTEM,
                        actor_id="integration-health",
                        action="integration.status_changed",
                        target_type="integration",
                        target_id=str(integration.id),
                        details={"from": previous.value, "to": health.status.value},
                    )
        for name, status in changed:
            log.info("integration_status_changed", integration=name, status=status)
        return changed
