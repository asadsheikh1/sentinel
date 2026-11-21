"""Metrics from Prometheus through fixed query templates."""

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

import httpx

from sentinel.telemetry.base import (
    InvalidQueryError,
    Limits,
    Result,
    Selector,
    TelemetryError,
    TimeRange,
    get_json,
    matchers,
)

_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]{0,199}$")


class Template(StrEnum):
    """The only metric questions SENTINEL can ask. ``{m}`` is the label matcher list."""

    REQUEST_RATE = "request_rate"
    ERROR_RATE = "error_rate"
    ERROR_RATIO = "error_ratio"
    LATENCY_P95 = "latency_p95"
    LATENCY_P99 = "latency_p99"
    SERIES = "series"  # a named metric, raw


_EXPR = {
    Template.REQUEST_RATE: (
        "sum by (service_name) (rate(http_server_request_duration_seconds_count{{{m}}}[5m]))"
    ),
    Template.ERROR_RATE: (
        "sum by (service_name) (rate(http_server_request_duration_seconds_count"
        '{{{m}, http_response_status_code=~"5.."}}[5m]))'
    ),
    Template.ERROR_RATIO: (
        "sum by (service_name) (rate(http_server_request_duration_seconds_count"
        '{{{m}, http_response_status_code=~"5.."}}[5m]))'
        " / sum by (service_name) (rate(http_server_request_duration_seconds_count{{{m}}}[5m]))"
    ),
    Template.LATENCY_P95: (
        "histogram_quantile(0.95, sum by (service_name, le) "
        "(rate(http_server_request_duration_seconds_bucket{{{m}}}[5m])))"
    ),
    Template.LATENCY_P99: (
        "histogram_quantile(0.99, sum by (service_name, le) "
        "(rate(http_server_request_duration_seconds_bucket{{{m}}}[5m])))"
    ),
}


def build_query(template: Template, selector: Selector, metric: str | None = None) -> str:
    m = matchers(selector)
    if template is Template.SERIES:
        if metric is None or not _METRIC_NAME.fullmatch(metric):
            raise InvalidQueryError("SERIES needs a valid metric name")
        return f"{metric}{{{m}}}"
    if metric is not None:
        raise InvalidQueryError("only SERIES takes a metric name")
    return _EXPR[template].format(m=m)


def step_seconds(window: TimeRange, max_points: int) -> int:
    """Smallest whole-second step that keeps each series under ``max_points``."""
    return max(15, math.ceil(window.seconds / max_points))


@dataclass
class MetricSeries:
    labels: dict[str, str]
    points: list[tuple[datetime, float]]


@dataclass
class MetricResult(Result):
    query: str = ""
    step_s: int = 0
    series: list[MetricSeries] = field(default_factory=list)


class PrometheusClient:
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

    async def __aenter__(self) -> "PrometheusClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def range(
        self,
        template: Template,
        selector: Selector,
        window: TimeRange,
        *,
        metric: str | None = None,
    ) -> MetricResult:
        query = build_query(template, selector, metric)
        step = step_seconds(window, self.limits.max_points)
        status, body = await get_json(
            self._client,
            "/api/v1/query_range",
            {
                "query": query,
                "start": f"{window.start.timestamp():.3f}",
                "end": f"{window.end.timestamp():.3f}",
                "step": str(step),
            },
            self.limits,
        )
        if status != 200 or not isinstance(body, dict) or body.get("status") != "success":
            error = body.get("error") if isinstance(body, dict) else None
            raise TelemetryError(f"prometheus rejected the query: {error or status}")
        raw = body["data"]["result"]
        result = MetricResult(query=query, step_s=step)
        if len(raw) > self.limits.max_series:
            result.truncated = True
            raw = raw[: self.limits.max_series]
        for item in raw:
            points = [
                (datetime.fromtimestamp(float(ts), UTC), float(value))
                for ts, value in item.get("values", [])
            ]
            if len(points) > self.limits.max_points:
                result.truncated = True
                points = points[-self.limits.max_points :]
            result.series.append(MetricSeries(labels=dict(item.get("metric", {})), points=points))
        return result
