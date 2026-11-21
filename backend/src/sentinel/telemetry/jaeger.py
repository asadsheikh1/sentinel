"""Traces from Jaeger's API v3 (OTLP JSON), flattened into spans. Attributes are cleaned."""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from sentinel.security.redaction import clean_value
from sentinel.telemetry.base import (
    InvalidQueryError,
    Limits,
    Result,
    TelemetryError,
    TimeRange,
    check_value,
    get_json,
)

_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
STATUS_ERROR = 2


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    service: str
    start: datetime
    duration_ms: float
    error: bool
    status_message: str | None
    attributes: dict[str, Any]


@dataclass
class Trace:
    trace_id: str
    spans: list[Span]

    @property
    def services(self) -> list[str]:
        return sorted({s.service for s in self.spans})

    @property
    def has_error(self) -> bool:
        return any(s.error for s in self.spans)

    @property
    def root(self) -> Span | None:
        roots = [s for s in self.spans if not s.parent_span_id]
        return min(roots or self.spans, key=lambda s: s.start, default=None)


@dataclass
class TraceResult(Result):
    traces: list[Trace] = field(default_factory=list)


def _value(v: dict[str, Any]) -> Any:
    for key in ("stringValue", "boolValue", "intValue", "doubleValue"):
        if key in v:
            return int(v[key]) if key == "intValue" else v[key]
    if "arrayValue" in v:
        return [_value(x) for x in v["arrayValue"].get("values", [])]
    return None


def _attrs(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {a["key"]: _value(a.get("value", {})) for a in items}


def parse_otlp(body: Any, limits: Limits) -> TraceResult:
    result = TraceResult()
    by_trace: dict[str, list[Span]] = {}
    for rs in (body or {}).get("result", {}).get("resourceSpans", []):
        resource = _attrs(rs.get("resource", {}).get("attributes", []))
        service = str(resource.get("service.name", "unknown"))
        for ss in rs.get("scopeSpans", []):
            for raw in ss.get("spans", []):
                status = raw.get("status", {})
                start_ns = int(raw.get("startTimeUnixNano", 0))
                end_ns = int(raw.get("endTimeUnixNano", start_ns))
                attributes = clean_value(_attrs(raw.get("attributes", [])), max_chars=500)
                trace_id = raw.get("traceId", "")
                by_trace.setdefault(trace_id, []).append(
                    Span(
                        trace_id=trace_id,
                        span_id=raw.get("spanId", ""),
                        parent_span_id=raw.get("parentSpanId") or None,
                        name=clean_value(raw.get("name", ""), max_chars=200),
                        service=clean_value(service, max_chars=200),
                        start=datetime.fromtimestamp(start_ns / 1e9, UTC),
                        duration_ms=(end_ns - start_ns) / 1e6,
                        error=status.get("code") == STATUS_ERROR,
                        status_message=clean_value(status.get("message"), max_chars=500)
                        if status.get("message")
                        else None,
                        attributes=attributes,
                    )
                )
    for trace_id, spans in by_trace.items():
        spans.sort(key=lambda s: s.start)
        if len(spans) > limits.max_spans_per_trace:
            result.truncated = True
            spans = spans[: limits.max_spans_per_trace]
        result.traces.append(Trace(trace_id=trace_id, spans=spans))
    result.traces.sort(
        key=lambda t: t.spans[0].start if t.spans else datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return result


class JaegerClient:
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

    async def __aenter__(self) -> "JaegerClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def trace(self, trace_id: str) -> Trace | None:
        if not _TRACE_ID.fullmatch(trace_id):
            raise InvalidQueryError("trace_id must be 32 lowercase hex characters")
        status, body = await get_json(self._client, f"/api/v3/traces/{trace_id}", {}, self.limits)
        if status == 404:
            return None
        if status != 200:
            raise TelemetryError(f"jaeger returned {status}")
        traces = parse_otlp(body, self.limits).traces
        return traces[0] if traces else None

    async def find(
        self, service: str, window: TimeRange, *, errors_only: bool = False, limit: int = 10
    ) -> TraceResult:
        """Recent traces that include ``service``. Newest first."""
        check_value("service", service)
        capped = max(1, min(limit, self.limits.max_traces))
        params = {
            "query.service_name": service,
            "query.start_time_min": window.start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "query.start_time_max": window.end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "query.search_depth": str(capped),
        }
        if errors_only:
            # Filter inside Jaeger. Filtering afterwards would miss errors older than
            # the newest `capped` traces. Jaeger marks error spans with error=true.
            params["query.attributes"] = '{"error":"true"}'
        status, body = await get_json(self._client, "/api/v3/traces", params, self.limits)
        if status == 404:  # Jaeger's answer for "no traces"
            return TraceResult()
        if status != 200:
            raise TelemetryError(f"jaeger returned {status}")
        result = parse_otlp(body, self.limits)
        if errors_only:  # backstop, in case the filter semantics ever change
            result.traces = [t for t in result.traces if t.has_error]
        if len(result.traces) > capped:
            result.truncated = True
            result.traces = result.traces[:capped]
        return result

    async def services(self) -> list[str]:
        status, body = await get_json(self._client, "/api/v3/services", {}, self.limits)
        if status != 200 or not isinstance(body, dict):
            raise TelemetryError(f"jaeger returned {status}")
        return sorted(str(s) for s in body.get("services", []))
