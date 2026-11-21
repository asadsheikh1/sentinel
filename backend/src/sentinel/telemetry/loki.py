"""Logs from Loki: selector + optional text filter + minimum severity. Every line is cleaned."""

from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from sentinel.security.redaction import clean, redact_text, strip_control, truncate
from sentinel.telemetry.base import (
    InvalidQueryError,
    Limits,
    Result,
    Selector,
    TelemetryError,
    TimeRange,
    escape,
    get_json,
    matchers,
)

# Labels worth keeping on each returned line; everything else is dropped.
KEEP_LABELS = (
    "service_name",
    "service_namespace",
    "host_name",
    "deployment_environment_name",
    "sentinel_integration_id",
    "severity_text",
    "severity_number",
    "trace_id",
    "span_id",
    "exc_type",
)
MAX_FILTER_CHARS = 200


@dataclass
class LogLine:
    ts: datetime
    line: str
    labels: dict[str, str]

    @property
    def trace_id(self) -> str | None:
        return self.labels.get("trace_id") or None


@dataclass
class LogResult(Result):
    query: str = ""
    lines: list[LogLine] = field(default_factory=list)


def build_query(selector: Selector, contains: str | None, min_severity: int | None) -> str:
    query = f"{{{matchers(selector)}}}"
    if contains is not None:
        if not 0 < len(contains) <= MAX_FILTER_CHARS or strip_control(contains) != contains:
            raise InvalidQueryError(
                f"text filter must be 1-{MAX_FILTER_CHARS} printable characters"
            )
        query += f' |= "{escape(contains)}"'
    if min_severity is not None:
        if not 1 <= min_severity <= 24:
            raise InvalidQueryError("min_severity must be an OTel SeverityNumber (1-24)")
        query += f" | severity_number >= {min_severity}"
    return query


class LokiClient:
    def __init__(
        self,
        base_url: str,
        limits: Limits | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.limits = limits or Limits()
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=self.limits.timeout_s, transport=transport
        )

    async def __aenter__(self) -> "LokiClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def logs(
        self,
        selector: Selector,
        window: TimeRange,
        *,
        contains: str | None = None,
        min_severity: int | None = None,
        limit: int = 100,
    ) -> LogResult:
        """Newest first. ``min_severity=17`` means ERROR and above."""
        query = build_query(selector, contains, min_severity)
        capped = max(1, min(limit, self.limits.max_log_lines))
        status, body = await get_json(
            self._client,
            "/loki/api/v1/query_range",
            {
                "query": query,
                "start": str(int(window.start.timestamp() * 1e9)),
                "end": str(int(window.end.timestamp() * 1e9)),
                "limit": str(capped),
                "direction": "backward",
            },
            self.limits,
        )
        if status != 200 or not isinstance(body, dict) or body.get("status") != "success":
            raise TelemetryError(f"loki rejected the query: {status}")
        result = LogResult(query=query)
        for stream in body["data"]["result"]:
            labels = {
                k: clean(v, max_chars=200)
                for k, v in stream.get("stream", {}).items()
                if k in KEEP_LABELS
            }
            for ts, raw_line in stream.get("values", []):
                redacted = redact_text(strip_control(raw_line))
                result.note_redactions(redacted.counts)
                line = truncate(redacted.text, self.limits.max_line_chars)
                if line != redacted.text:
                    result.truncated = True
                result.lines.append(
                    LogLine(ts=datetime.fromtimestamp(int(ts) / 1e9, UTC), line=line, labels=labels)
                )
        result.lines.sort(key=lambda entry: entry.ts, reverse=True)
        if len(result.lines) >= capped:
            result.truncated = True
        result.lines = result.lines[:capped]
        return result
