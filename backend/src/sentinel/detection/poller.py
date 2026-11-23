"""The Loki error poller: read error lines since the watermark, fold them into groups.

Restart safety: one pass is one database transaction. The groups, their
signals and the new watermark are committed together, so a pass that fails
(Loki down, a crash, a restart) commits nothing and the next pass reads the
same window again: every line is counted exactly once.

- **Window:** ``[watermark, now - lag)``. The lag lets lines that arrive a
  little late (batching, agent retries) land before their minute is read.
  The first pass starts ``error_poll_lookback_s`` back, not at the beginning
  of time.
- **Paging:** forward, oldest first. A full page ends at its last timestamp,
  and that timestamp's lines are read again on the next page, so lines that
  share a nanosecond are neither lost nor counted twice. If the page budget
  runs out, the watermark stops where reading stopped, and the next pass
  continues from there.
- **One poller:** a transaction-scoped advisory lock; a second API process
  skips its pass.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sentinel.config import Settings
from sentinel.db.engine import session_scope
from sentinel.db.models import PollWatermark
from sentinel.detection.error_groups import ApplyResult, apply, classify_line
from sentinel.telemetry.loki import LogLine, LokiClient

log = structlog.get_logger("sentinel.detection")

SOURCE = "loki_errors"
LOCK_KEY = 0x5E7_11E1  # pg advisory lock id for this poller
# Every source SENTINEL ingests carries sentinel_integration_id (set at the gateway).
QUERIES = (
    # Records that say they're errors, or carry an exception.
    '{sentinel_integration_id=~".+"} | severity_number >= 17 or exception_type != ""',
    # Lines that don't say so but are: a traceback, or a JSON logger's level, read
    # from a file or the journal (container stdout arrives as INFO).
    '{sentinel_integration_id=~".+"} |~ `Traceback \\(most recent call last\\)'
    '|"(?:level|levelname|severity)": ?"(?i:error|critical|fatal)"`',
)


@dataclass
class PassResult:
    skipped: str | None = None
    start: datetime | None = None
    until: datetime | None = None
    read: int = 0
    applied: ApplyResult | None = None


def _dt(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, UTC)


async def read_window(
    loki: LokiClient, query: str, start_ns: int, end_ns: int, *, page: int, max_pages: int
) -> tuple[list[LogLine], int]:
    """Lines in ``[start_ns, reached)``, and ``reached`` (== end_ns unless the budget ran out)."""
    out: list[LogLine] = []
    cursor = start_ns
    for _ in range(max_pages):
        lines, full = await loki.entries(query, cursor, end_ns, limit=page)
        if not full or not lines:
            out.extend(lines)
            return out, end_ns
        last = lines[-1].ns
        if last <= cursor:
            # A whole page at one nanosecond: take it and step past (more lines at
            # exactly that instant than a page holds are dropped; logged).
            log.warning("error_poll_page_one_timestamp", ns=last, page=page)
            out.extend(lines)
            cursor = last + 1
            continue
        out.extend(line for line in lines if line.ns < last)
        cursor = last
    return out, cursor


def _identity(line: LogLine) -> tuple[int, str, tuple[tuple[str, str], ...]]:
    return line.ns, line.line, tuple(sorted(line.labels.items()))


async def poll_pass(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    loki_factory: Callable[[], LokiClient] | None = None,
    now: datetime | None = None,
    source: str = SOURCE,
) -> PassResult:
    now = now or datetime.now(UTC)
    end_ns = int((now.timestamp() - settings.error_poll_lag_s) * 1e9)
    factory = loki_factory or (lambda: LokiClient(settings.loki_url))
    async with session_scope(session_factory) as db:
        if not await db.scalar(select(func.pg_try_advisory_xact_lock(LOCK_KEY))):
            return PassResult(skipped="another poller holds the lock")
        mark = await db.get(PollWatermark, source, with_for_update=True)
        start_ns = mark.last_ns if mark else end_ns - int(settings.error_poll_lookback_s * 1e9)
        if start_ns >= end_ns:
            return PassResult(skipped="nothing new to read")
        reached = end_ns
        lines: dict[tuple[int, str, tuple[tuple[str, str], ...]], LogLine] = {}
        async with factory() as loki:
            for query in QUERIES:
                got, upto = await read_window(
                    loki,
                    query,
                    start_ns,
                    end_ns,
                    page=settings.error_poll_page_size,
                    max_pages=settings.error_poll_max_pages,
                )
                reached = min(reached, upto)
                for line in got:
                    lines.setdefault(_identity(line), line)
        # Only what *every* query has read completely.
        batch = sorted((x for x in lines.values() if x.ns < reached), key=lambda x: x.ns)
        occurrences = [o for line in batch if (o := classify_line(line)) is not None]
        until = _dt(reached)
        applied = await apply(db, occurrences, until=until, settings=settings)
        if mark is None:
            db.add(PollWatermark(source=source, last_ns=reached, last_ts=until))
        else:
            mark.last_ns = reached
            mark.last_ts = until
            mark.updated_at = datetime.now(UTC)
    if applied.groups_new or applied.signals_opened or applied.signals_resolved:
        log.info(
            "error_poll",
            read=len(batch),
            errors=applied.lines,
            groups_new=applied.groups_new,
            opened=len(applied.signals_opened),
            resolved=len(applied.signals_resolved),
        )
    return PassResult(start=_dt(start_ns), until=until, read=len(batch), applied=applied)
