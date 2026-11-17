"""Push synthetic OTLP straight into each store and read it back (Step 5 acceptance).

Needs the stores running: `make obs-up`, then `make test-obs`. Endpoints can be
overridden with SENTINEL_TEST_{PROMETHEUS,LOKI,JAEGER,ALERTMANAGER}_URL.
"""

import os
import secrets
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx2 as httpx
import pytest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord, ResourceLogs, ScopeLogs
from opentelemetry.proto.metrics.v1.metrics_pb2 import (
    Gauge,
    Metric,
    NumberDataPoint,
    ResourceMetrics,
    ScopeMetrics,
)
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

pytestmark = pytest.mark.observability

PROM = os.environ.get("SENTINEL_TEST_PROMETHEUS_URL", "http://127.0.0.1:9090")
LOKI = os.environ.get("SENTINEL_TEST_LOKI_URL", "http://127.0.0.1:3100")
JAEGER = os.environ.get("SENTINEL_TEST_JAEGER_URL", "http://127.0.0.1:16686")
JAEGER_OTLP = os.environ.get("SENTINEL_TEST_JAEGER_OTLP_URL", "http://127.0.0.1:4318")
ALERTMANAGER = os.environ.get("SENTINEL_TEST_ALERTMANAGER_URL", "http://127.0.0.1:9093")
PROTOBUF = {"Content-Type": "application/x-protobuf"}


def _kv(key: str, value: str) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(string_value=value))


def _resource(run_id: str) -> Resource:
    return Resource(
        attributes=[
            _kv("service.name", "sentinel-selftest"),
            _kv("service.namespace", "sentinel"),
            _kv("sentinel.integration.id", "int-selftest"),
            _kv("run.id", run_id),
        ]
    )


def _now_ns() -> int:
    return time.time_ns()


def eventually(fetch: Callable[[], Any], timeout: float = 60, every: float = 2) -> Any:
    """Poll until fetch() returns something truthy (stores ingest asynchronously)."""
    end = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < end:
        try:
            last = fetch()
            if last:
                return last
        except httpx.HTTPError:
            pass
        time.sleep(every)
    pytest.fail(f"not visible after {timeout}s; last={last!r}")


@pytest.fixture(scope="module")
def http() -> httpx.Client:
    return httpx.Client(timeout=15)


@pytest.fixture
def run_id() -> str:
    return uuid.uuid4().hex[:12]


def test_prometheus_accepts_otlp_metrics_and_answers_queries(
    http: httpx.Client, run_id: str
) -> None:
    point = NumberDataPoint(
        time_unix_nano=_now_ns(), as_double=42.5, attributes=[_kv("check", run_id)]
    )
    req = ExportMetricsServiceRequest(
        resource_metrics=[
            ResourceMetrics(
                resource=_resource(run_id),
                scope_metrics=[
                    ScopeMetrics(
                        metrics=[
                            Metric(
                                name="sentinel.selftest.value",
                                unit="1",
                                gauge=Gauge(data_points=[point]),
                            )
                        ]
                    )
                ],
            )
        ]
    )
    r = http.post(
        f"{PROM}/api/v1/otlp/v1/metrics", content=req.SerializeToString(), headers=PROTOBUF
    )
    assert r.status_code == 200, r.text

    def query() -> list[dict[str, Any]]:
        q = f'sentinel_selftest_value_ratio{{check="{run_id}"}}'
        res = http.get(f"{PROM}/api/v1/query", params={"query": q}).json()
        return res["data"]["result"]  # type: ignore[no-any-return]

    series = eventually(query)
    labels = series[0]["metric"]
    assert float(series[0]["value"][1]) == 42.5
    assert labels["job"] == "sentinel/sentinel-selftest"  # service.namespace/service.name
    assert labels["service_name"] == "sentinel-selftest"  # promoted resource attribute
    assert labels["sentinel_integration_id"] == "int-selftest"


def test_prometheus_loaded_red_rules(http: httpx.Client) -> None:
    groups = http.get(f"{PROM}/api/v1/rules").json()["data"]["groups"]
    names = {rule["name"] for g in groups for rule in g["rules"]}
    assert {
        "service:http_requests:rate5m",
        "service:http_error_ratio:rate5m",
        "service:http_latency_p95_seconds:5m",
    } <= names
    assert all(rule["health"] == "ok" for g in groups for rule in g["rules"])


def test_loki_accepts_otlp_logs_and_answers_queries(http: httpx.Client, run_id: str) -> None:
    trace_id = secrets.token_bytes(16)
    record = LogRecord(
        time_unix_nano=_now_ns(),
        severity_text="ERROR",
        body=AnyValue(string_value=f"selftest log line {run_id}"),
        trace_id=trace_id,
        attributes=[_kv("check", run_id)],
    )
    req = ExportLogsServiceRequest(
        resource_logs=[
            ResourceLogs(resource=_resource(run_id), scope_logs=[ScopeLogs(log_records=[record])])
        ]
    )
    r = http.post(f"{LOKI}/otlp/v1/logs", content=req.SerializeToString(), headers=PROTOBUF)
    assert r.status_code in (200, 204), r.text

    def query() -> list[dict[str, Any]]:
        params = {
            "query": f'{{service_name="sentinel-selftest"}} |= "{run_id}"',
            "start": str(_now_ns() - 10 * 60 * 10**9),
            "end": str(_now_ns() + 60 * 10**9),
            "limit": "10",
        }
        res = http.get(f"{LOKI}/loki/api/v1/query_range", params=params).json()
        return res["data"]["result"]  # type: ignore[no-any-return]

    streams = eventually(query)
    stream = streams[0]["stream"]
    assert stream["service_name"] == "sentinel-selftest"
    assert stream["sentinel_integration_id"] == "int-selftest"  # our extra index label
    line = streams[0]["values"][0]
    assert run_id in line[1]
    # trace_id travels as structured metadata (shown in the stream labels on query)
    assert stream.get("trace_id") == trace_id.hex()


def test_jaeger_accepts_otlp_traces_and_answers_queries(http: httpx.Client, run_id: str) -> None:
    trace_id = secrets.token_bytes(16)
    start = _now_ns()
    span = Span(
        trace_id=trace_id,
        span_id=secrets.token_bytes(8),
        name=f"selftest-{run_id}",
        kind=Span.SPAN_KIND_SERVER,
        start_time_unix_nano=start,
        end_time_unix_nano=start + 5_000_000,
        attributes=[_kv("check", run_id)],
    )
    req = ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(resource=_resource(run_id), scope_spans=[ScopeSpans(spans=[span])])
        ]
    )
    r = http.post(f"{JAEGER_OTLP}/v1/traces", content=req.SerializeToString(), headers=PROTOBUF)
    assert r.status_code == 200, r.text

    def fetch() -> dict[str, Any] | None:
        res = http.get(f"{JAEGER}/api/v3/traces/{trace_id.hex()}")
        return res.json() if res.status_code == 200 else None

    body = eventually(fetch)
    spans = [
        s for rs in body["result"]["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]
    ]
    assert [s["name"] for s in spans] == [f"selftest-{run_id}"]

    services = http.get(f"{JAEGER}/api/v3/services").json()["services"]
    assert "sentinel-selftest" in services


def test_alertmanager_accepts_and_lists_alerts(http: httpx.Client, run_id: str) -> None:
    alert = [{"labels": {"alertname": "SentinelSelfTest", "run_id": run_id}, "annotations": {}}]
    assert http.post(f"{ALERTMANAGER}/api/v2/alerts", json=alert).status_code == 200

    def listed() -> bool:
        alerts = http.get(f"{ALERTMANAGER}/api/v2/alerts").json()
        return any(a["labels"].get("run_id") == run_id for a in alerts)

    assert eventually(listed, timeout=30)
