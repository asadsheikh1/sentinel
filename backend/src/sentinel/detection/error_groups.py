"""Error groups: error log lines → counted groups → ``error_group_*`` signals.

One line becomes an *occurrence* (``classify_line``, pure): its scope (the
service, or the host's container or unit), its level, and its fingerprint.
``apply`` folds a batch of occurrences into ``error_groups`` and opens or
resolves signals:

- ``error_group_new`` when a fingerprint is seen for the first time in a scope.
  It resolves once the group has been quiet for ``error_group_quiet_s``.
- ``error_group_spike`` when an older group (≥ 15 min) logs at least
  ``error_spike_min_count`` in 5 minutes *and* ``error_spike_factor`` times its
  average 5 minutes over the hour before. It resolves when that stops.

Time is log time: "now" is how far the poller has read, so a pass that catches
up on an hour of logs judges each minute the same as a live pass would.
"""

import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.config import Settings
from sentinel.db.models import ErrorGroup, Integration, Signal, SignalKind, SignalStatus
from sentinel.detection.fingerprint import (
    ErrorKey,
    app_packages,
    fingerprint,
    frame_key,
    normalise_path,
    top_in_app_frame,
)
from sentinel.detection.traceback import ParsedTraceback, parse
from sentinel.integrations.entities import resolve_entity
from sentinel.telemetry.loki import LogLine

log = structlog.get_logger("sentinel.detection")

MINUTES_KEPT = 70
SPIKE_WINDOW_MIN = 5
BASELINE_MIN = 60
SPIKE_MIN_AGE = timedelta(minutes=15)
MAX_TRACE_IDS = 5

# ------------------------------------------------------------------ one line

_JSON_LEVEL_KEYS = ("level", "levelname", "severity", "log.level", "lvl")
_JSON_MESSAGE_KEYS = ("msg", "message", "event", "error")
_JSON_STACK_KEYS = ("stack", "stack_trace", "stacktrace", "exc_info", "traceback", "exception")
_JSON_TYPE_KEYS = ("exc_type", "exception_type", "error_type")
_TEXT_LEVEL = re.compile(
    r"\b(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|ERR|CRITICAL|CRIT|FATAL|PANIC|EMERG|ALERT)\b"
    r"|\blevel=(\w+)",
)
_LEVELS = {
    "error": "error", "err": "error",
    "critical": "critical", "crit": "critical", "fatal": "critical", "panic": "critical",
    "emerg": "critical", "alert": "critical",
}  # fmt: skip
_NOT_ERROR = frozenset({"trace", "debug", "info", "notice", "warn", "warning"})


@dataclass(frozen=True)
class Scope:
    key: str
    label: str  # for titles
    service_name: str | None


@dataclass(frozen=True)
class Occurrence:
    scope: Scope
    key: ErrorKey
    level: str  # "error" | "critical"
    ts: datetime
    trace_id: str | None
    labels: Mapping[str, str]
    sample: dict[str, Any]


def scope_of(labels: Mapping[str, str]) -> Scope:
    host = labels.get("host_name") or ""
    service = labels.get("service_name") or ""
    if service and not service.startswith("unknown_service"):
        ns = labels.get("service_namespace", "")
        env = labels.get("deployment_environment_name", "")
        return Scope(f"service:{ns}/{service}@{env}", service, service)
    if container := labels.get("container_name"):
        return Scope(f"container:{host}/{container}", f"{host}/{container}", None)
    if unit := labels.get("systemd_unit") or labels.get("syslog_identifier"):
        return Scope(f"unit:{host}/{unit}", f"{host}/{unit}", None)
    if host:
        return Scope(f"host:{host}", host, None)
    return Scope(f"integration:{labels.get('sentinel_integration_id', '')}", "unknown source", None)


def _json_body(line: str) -> dict[str, Any] | None:
    text = line.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        body = json.loads(text)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _first(body: Mapping[str, Any] | None, keys: Iterable[str]) -> str | None:
    if not body:
        return None
    for key in keys:
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _level(labels: Mapping[str, str], body: Mapping[str, Any] | None, line: str) -> str | None:
    """ "error", "critical", or None (not an error).

    An SDK record (it says where it was logged) has a real severity. A line read
    from a file or the journal doesn't: stderr is "error" even for INFO lines, so
    the line's own level, when it has one, wins.
    """
    sdk = "code_file_path" in labels or "scope_name" in labels
    if not sdk:
        if (word := _first(body, _JSON_LEVEL_KEYS)) is not None:
            return _LEVELS.get(word.lower())
        if body is None and (match := _TEXT_LEVEL.search(line[:120])):
            word = (match[1] or match[2] or "").lower()
            if word in _LEVELS or word in _NOT_ERROR:
                return _LEVELS.get(word)
    try:
        number = int(labels.get("severity_number", "0"))
    except ValueError:
        number = 0
    if number >= 21:
        return "critical"
    if number >= 17:
        return "error"
    if not sdk and labels.get("severity_text", "").lower() in _LEVELS:
        return _LEVELS[labels["severity_text"].lower()]
    return None


def classify_line(line: LogLine) -> Occurrence | None:
    """One error occurrence, or None for a line that isn't an error. Pure."""
    labels = line.labels
    body = _json_body(line.line)
    parsed: ParsedTraceback | None = None
    for text in (labels.get("exception_stacktrace"), _first(body, _JSON_STACK_KEYS), line.line):
        if text and (parsed := parse(text)) is not None:
            break
    level = _level(labels, body, line.line)
    if level is None:
        if parsed is None or parsed.exc_type is None:
            return None
        level = "error"  # an uncaught traceback, whatever the line's level says

    exc_type = labels.get("exception_type") or (parsed.exc_type if parsed else None)
    exc_type = exc_type or _first(body, _JSON_TYPE_KEYS)
    if exc_type:
        message = labels.get("exception_message")
        if message is None:
            message = parsed.message if parsed and parsed.exc_type else ""
    else:
        message = _first(body, _JSON_MESSAGE_KEYS) or line.line
    logger = labels.get("scope_name") or _first(body, ("logger", "name", "logger_name"))
    where = ""
    if parsed and parsed.frames:
        packages = app_packages(code_file_path=labels.get("code_file_path"), logger=logger)
        top = top_in_app_frame(parsed.frames, packages)
        where = frame_key(top) if top else ""
    elif path := labels.get("code_file_path"):
        func = labels.get("code_function_name", "")
        where = f"{normalise_path(path)}:{func}" if func else normalise_path(path)
    elif logger:
        where = logger

    key = fingerprint(exc_type, message, where)
    trace_id = labels.get("trace_id") or (body or {}).get("trace_id")
    sample = {
        "ts": line.ts.isoformat(),
        "line": line.line[:2000],
        "exception_type": key.exception_type,
        "message": message[:1000],
        "stacktrace": (labels.get("exception_stacktrace") or _first(body, _JSON_STACK_KEYS) or "")[
            -8000:
        ],
        "trace_id": trace_id if isinstance(trace_id, str) else None,
    }
    return Occurrence(
        scope=scope_of(labels),
        key=key,
        level=level,
        ts=line.ts,
        trace_id=sample["trace_id"],
        labels=labels,
        sample=sample,
    )


# ------------------------------------------------------------------ a batch


@dataclass
class _Batch:
    first: Occurrence
    last: Occurrence
    count: int = 0
    minutes: dict[str, int] = field(default_factory=dict)
    trace_ids: list[str] = field(default_factory=list)
    level: str = "error"


def _minute(ts: datetime) -> int:
    return int(ts.timestamp() // 60)


def batch(occurrences: Iterable[Occurrence]) -> dict[tuple[str, str], _Batch]:
    out: dict[tuple[str, str], _Batch] = {}
    for occ in sorted(occurrences, key=lambda o: o.ts):
        b = out.setdefault((occ.scope.key, occ.key.fingerprint), _Batch(first=occ, last=occ))
        b.count += 1
        b.last = occ
        minute = str(_minute(occ.ts))
        b.minutes[minute] = b.minutes.get(minute, 0) + 1
        if occ.trace_id and occ.trace_id not in b.trace_ids:
            b.trace_ids.append(occ.trace_id)
        if occ.level == "critical":
            b.level = "critical"
    return out


def spike_ratio(minute_counts: Mapping[str, int], until: datetime) -> tuple[int, float]:
    """(count in the last 5 minutes, average 5-minute count over the hour before)."""
    end = _minute(until)
    current = sum(n for m, n in minute_counts.items() if end - SPIKE_WINDOW_MIN < int(m) <= end)
    base_start = end - SPIKE_WINDOW_MIN - BASELINE_MIN
    base = sum(n for m, n in minute_counts.items() if base_start < int(m) <= end - SPIKE_WINDOW_MIN)
    return current, base / (BASELINE_MIN / SPIKE_WINDOW_MIN)


def is_spiking(group: ErrorGroup, until: datetime, settings: Settings) -> tuple[bool, int, float]:
    current, baseline = spike_ratio(group.minute_counts, until)
    old_enough = group.first_seen <= until - SPIKE_MIN_AGE
    spiking = (
        old_enough
        and current >= settings.error_spike_min_count
        and current >= settings.error_spike_factor * max(baseline, 1.0)
    )
    return spiking, current, baseline


def _short(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _what(group: ErrorGroup) -> str:
    what = (
        f"{group.exception_type}: {group.normalized_message}"
        if group.exception_type
        else group.normalized_message
    )
    return _short(what, 140)


@dataclass
class ApplyResult:
    lines: int = 0
    groups_new: int = 0
    groups_updated: int = 0
    signals_opened: list[str] = field(default_factory=list)
    signals_resolved: list[str] = field(default_factory=list)


class _Lookups:
    """Per-pass caches: entity by scope, integration existence."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.entities: dict[str, uuid.UUID | None] = {}
        self.integrations: dict[str, uuid.UUID | None] = {}

    async def entity(self, occ: Occurrence) -> uuid.UUID | None:
        if occ.scope.key not in self.entities:
            self.entities[occ.scope.key] = await resolve_entity(self.db, occ.labels)
        return self.entities[occ.scope.key]

    async def integration(self, labels: Mapping[str, str]) -> uuid.UUID | None:
        raw = labels.get("sentinel_integration_id", "")
        if raw not in self.integrations:
            try:
                iid: uuid.UUID | None = uuid.UUID(raw)
            except ValueError:
                iid = None
            if iid is not None and await self.db.get(Integration, iid) is None:
                iid = None  # deleted since: don't link
            self.integrations[raw] = iid
        return self.integrations[raw]


def _trim(minutes: Mapping[str, int], until: datetime) -> dict[str, int]:
    oldest = _minute(until) - MINUTES_KEPT
    return {m: n for m, n in minutes.items() if int(m) > oldest}


async def apply(
    db: AsyncSession, occurrences: Iterable[Occurrence], *, until: datetime, settings: Settings
) -> ApplyResult:
    """Fold occurrences (all before ``until``) into groups; open and resolve signals."""
    result = ApplyResult()
    lookups = _Lookups(db)
    touched: list[ErrorGroup] = []
    batches = batch(occurrences)
    for (scope_key, fp), b in batches.items():
        result.lines += b.count
        group = await db.scalar(
            select(ErrorGroup).where(
                ErrorGroup.scope_key == scope_key, ErrorGroup.fingerprint == fp
            )
        )
        if group is None:
            group = ErrorGroup(
                scope_key=scope_key,
                entity_id=await lookups.entity(b.first),
                service_name=b.first.scope.service_name,
                source_integration_id=await lookups.integration(b.first.labels),
                fingerprint=fp,
                exception_type=b.first.key.exception_type,
                normalized_message=b.first.key.message,
                top_frame=b.first.key.top_frame,
                level=b.level,
                first_seen=b.first.ts,
                last_seen=b.last.ts,
                total_count=b.count,
                minute_counts=_trim(b.minutes, until),
                sample_trace_ids=b.trace_ids[-MAX_TRACE_IDS:],
                sample_log=b.last.sample,
            )
            db.add(group)
            await db.flush()
            result.groups_new += 1
            db.add(_new_signal(group, b.first.scope.label))
            result.signals_opened.append(f"new:{scope_key}:{group.top_frame}")
            log.info("error_group_new", scope=scope_key, top_frame=group.top_frame)
        else:
            merged = dict(group.minute_counts)
            for minute, n in b.minutes.items():
                merged[minute] = merged.get(minute, 0) + n
            group.minute_counts = _trim(merged, until)
            group.total_count += b.count
            group.last_seen = max(group.last_seen, b.last.ts)
            group.first_seen = min(group.first_seen, b.first.ts)
            if b.level == "critical":
                group.level = "critical"
            group.sample_trace_ids = [t for t in [*group.sample_trace_ids, *b.trace_ids] if t][
                -MAX_TRACE_IDS:
            ]
            group.sample_log = b.last.sample
            group.updated_at = datetime.now(UTC)
            if group.entity_id is None:
                group.entity_id = await lookups.entity(b.first)
            result.groups_updated += 1
        touched.append(group)
    await db.flush()
    await _spikes(db, touched, until, settings, result)
    await _resolve_quiet(db, until, settings, result)
    return result


def _new_signal(group: ErrorGroup, label: str) -> Signal:
    where = f" at {group.top_frame}" if group.top_frame else ""
    return Signal(
        kind=SignalKind.ERROR_GROUP_NEW,
        status=SignalStatus.FIRING,
        severity="critical" if group.level == "critical" else "warning",
        title=_short(f"{label}: new error {_what(group)}{where}", 300),
        entity_id=group.entity_id,
        service_name=group.service_name,
        source_integration_id=group.source_integration_id,
        fingerprint=group.fingerprint,
        labels=_signal_labels(group),
        payload={"error_group_id": str(group.id), "first_count": group.total_count},
        starts_at=group.first_seen,
        dedupe_key=f"eg:new:{group.id}",
    )


def _signal_labels(group: ErrorGroup) -> dict[str, str]:
    return {
        "error_group_id": str(group.id),
        "scope": group.scope_key,
        "exception_type": group.exception_type,
        "top_frame": group.top_frame,
    }


async def _open_spike(db: AsyncSession, group: ErrorGroup) -> Signal | None:
    found: Signal | None = await db.scalar(
        select(Signal).where(
            Signal.kind == SignalKind.ERROR_GROUP_SPIKE,
            Signal.status == SignalStatus.FIRING,
            Signal.fingerprint == group.fingerprint,
            Signal.labels["error_group_id"].astext == str(group.id),
        )
    )
    return found


async def _spikes(
    db: AsyncSession,
    touched: list[ErrorGroup],
    until: datetime,
    settings: Settings,
    result: ApplyResult,
) -> None:
    # Groups that just logged, plus any with a spike still open (it may have stopped).
    open_ids = {
        uuid.UUID(s.labels["error_group_id"])
        for s in (
            await db.scalars(
                select(Signal).where(
                    Signal.kind == SignalKind.ERROR_GROUP_SPIKE,
                    Signal.status == SignalStatus.FIRING,
                )
            )
        ).all()
    }
    groups = {g.id: g for g in touched}
    for gid in open_ids - groups.keys():
        if (g := await db.get(ErrorGroup, gid)) is not None:
            groups[gid] = g
    for group in groups.values():
        spiking, current, baseline = is_spiking(group, until, settings)
        signal = await _open_spike(db, group) if group.id in open_ids or spiking else None
        if spiking and signal is None:
            ratio = current / max(baseline, 1.0)
            label = group.service_name or group.scope_key.split(":", 1)[-1]
            db.add(
                Signal(
                    kind=SignalKind.ERROR_GROUP_SPIKE,
                    status=SignalStatus.FIRING,
                    severity="critical" if group.level == "critical" else "warning",
                    title=_short(
                        f"{label}: error spike ({current} in 5 min, {ratio:.0f}x usual) "
                        f"{_what(group)}",
                        300,
                    ),
                    entity_id=group.entity_id,
                    service_name=group.service_name,
                    source_integration_id=group.source_integration_id,
                    fingerprint=group.fingerprint,
                    labels=_signal_labels(group),
                    payload={
                        "error_group_id": str(group.id),
                        "count_5m": current,
                        "baseline_5m": round(baseline, 2),
                    },
                    starts_at=until,
                    dedupe_key=f"eg:spike:{group.id}:{int(until.timestamp())}",
                )
            )
            result.signals_opened.append(f"spike:{group.scope_key}:{group.top_frame}")
            log.info("error_group_spike", scope=group.scope_key, count_5m=current)
        elif not spiking and signal is not None:
            _resolve(signal, until, "back to normal")
            result.signals_resolved.append(f"spike:{group.scope_key}:{group.top_frame}")


def _resolve(signal: Signal, at: datetime, because: str) -> None:
    signal.status = SignalStatus.RESOLVED
    signal.ends_at = at
    signal.updated_at = datetime.now(UTC)
    signal.payload = {**signal.payload, "resolved_because": because}


async def _resolve_quiet(
    db: AsyncSession, until: datetime, settings: Settings, result: ApplyResult
) -> None:
    """A "new error" signal resolves once its group has been quiet for a while."""
    quiet_since = until - timedelta(seconds=settings.error_group_quiet_s)
    group_id = Signal.labels["error_group_id"].astext.cast(ErrorGroup.id.type)
    rows = await db.execute(
        select(Signal, ErrorGroup)
        .join(ErrorGroup, ErrorGroup.id == group_id)
        .where(
            Signal.kind == SignalKind.ERROR_GROUP_NEW,
            Signal.status == SignalStatus.FIRING,
            ErrorGroup.last_seen < quiet_since,
        )
    )
    for signal, group in rows.all():
        _resolve(signal, until, f"no occurrences for {int(settings.error_group_quiet_s)} s")
        result.signals_resolved.append(f"new:{group.scope_key}:{group.top_frame}")
