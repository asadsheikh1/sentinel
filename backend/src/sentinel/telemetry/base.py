"""Shared types for the telemetry adapters: time windows, selectors, limits, errors.

Callers never pass query strings. They pass a ``Selector`` (who) and a
``TimeRange`` (when). The adapters build the query from fixed templates, so
values chosen elsewhere (later: by the LLM's follow-up requests) can't inject
query syntax.
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

MAX_WINDOW = timedelta(days=7)
_SAFE_VALUE = re.compile(r"^[\w .:/@+-]{1,200}$")


class TelemetryError(Exception):
    """A telemetry backend failed or returned something unusable."""


class TelemetryTimeoutError(TelemetryError):
    pass


class InvalidQueryError(ValueError):
    """A selector, window or parameter was rejected before any request was made."""


@dataclass(frozen=True)
class TimeRange:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise InvalidQueryError("time range must be timezone-aware")
        if self.end <= self.start:
            raise InvalidQueryError("time range end must be after start")
        if self.end - self.start > MAX_WINDOW:
            raise InvalidQueryError(f"time range longer than {MAX_WINDOW.days} days")

    @classmethod
    def last(cls, minutes: float, *, now: datetime | None = None) -> "TimeRange":
        end = now or datetime.now(UTC)
        return cls(end - timedelta(minutes=minutes), end)

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


def check_value(name: str, value: str) -> str:
    """Selector values are identifiers, never free text."""
    if not _SAFE_VALUE.fullmatch(value):
        raise InvalidQueryError(f"{name} contains characters that are not allowed")
    return value


@dataclass(frozen=True)
class Selector:
    """Which telemetry. Every field is optional; at least one must be set."""

    service: str | None = None
    namespace: str | None = None
    environment: str | None = None
    host: str | None = None
    integration_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        for name in ("service", "namespace", "environment", "host"):
            value = getattr(self, name)
            if value is not None:
                check_value(name, value)
        if not self.labels():
            raise InvalidQueryError("a selector needs at least one field")

    def labels(self) -> dict[str, str]:
        """Prometheus and Loki label names (both use the same translated names)."""
        pairs = {
            "service_name": self.service,
            "service_namespace": self.namespace,
            "deployment_environment_name": self.environment,
            "host_name": self.host,
            "sentinel_integration_id": str(self.integration_id) if self.integration_id else None,
        }
        return {k: v for k, v in pairs.items() if v is not None}


def escape(value: str) -> str:
    """Escape a value for a double-quoted PromQL or LogQL string."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def matchers(selector: Selector, extra: dict[str, str] | None = None) -> str:
    labels = {**selector.labels(), **(extra or {})}
    return ", ".join(f'{name}="{escape(value)}"' for name, value in sorted(labels.items()))


@dataclass(frozen=True)
class Limits:
    timeout_s: float = 10.0
    max_response_bytes: int = 5 * 1024 * 1024
    max_series: int = 50
    max_points: int = 600
    max_log_lines: int = 500
    max_line_chars: int = 2000
    max_traces: int = 20
    max_spans_per_trace: int = 500


@dataclass
class Result:
    """Every adapter result says whether caps cut it short, and what was redacted."""

    truncated: bool = False
    redactions: dict[str, int] = field(default_factory=dict)

    def note_redactions(self, counts: dict[str, int]) -> None:
        for kind, n in counts.items():
            self.redactions[kind] = self.redactions.get(kind, 0) + n


async def get_json(
    client: httpx.AsyncClient, path: str, params: dict[str, Any], limits: Limits
) -> tuple[int, Any]:
    """GET with a byte cap. Returns (status, parsed JSON). Never raises for 4xx."""
    try:
        async with client.stream("GET", path, params=params) as response:
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > limits.max_response_bytes:
                    raise TelemetryError(
                        f"response larger than {limits.max_response_bytes} bytes; narrow the query"
                    )
                chunks.append(chunk)
            status = response.status_code
    except httpx.TimeoutException as e:
        raise TelemetryTimeoutError(f"{client.base_url} timed out") from e
    except httpx.HTTPError as e:
        raise TelemetryError(f"{client.base_url} unreachable: {type(e).__name__}") from e
    body = b"".join(chunks)
    if status >= 500:
        raise TelemetryError(f"{client.base_url}{path} returned {status}")
    try:
        import json

        return status, json.loads(body) if body else None
    except ValueError as e:
        raise TelemetryError(f"{client.base_url}{path} returned invalid JSON") from e
