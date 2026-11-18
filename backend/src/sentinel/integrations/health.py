"""Integration health: a pure function from what was last seen to a status."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from sentinel.db.models import IntegrationStatus


@dataclass(frozen=True)
class Health:
    status: IntegrationStatus
    reason: str | None


def compute_health(
    current: IntegrationStatus,
    expected_signals: Iterable[str],
    last_seen: Mapping[str, datetime],
    *,
    now: datetime,
    stale_after: timedelta,
    series_count: int = 0,
    max_series: int | None = None,
) -> Health:
    """Decide an integration's status.

    ``disabled`` and ``revoked`` are set by people and never change here.
    """
    if current in (IntegrationStatus.DISABLED, IntegrationStatus.REVOKED):
        return Health(current, None)
    if not last_seen:
        return Health(IntegrationStatus.PENDING, "No telemetry received yet.")

    fresh = {signal for signal, seen in last_seen.items() if now - seen < stale_after}
    if not fresh:
        seconds = int(stale_after.total_seconds())
        return Health(IntegrationStatus.STALE, f"Nothing received for over {seconds} seconds.")

    missing = sorted(set(expected_signals) - fresh)
    if missing:
        return Health(
            IntegrationStatus.DEGRADED, f"Expected signals missing: {', '.join(missing)}."
        )
    if max_series is not None and series_count > max_series:
        return Health(
            IntegrationStatus.DEGRADED,
            f"Too many metric series: {series_count} (limit {max_series}).",
        )
    return Health(IntegrationStatus.HEALTHY, None)
