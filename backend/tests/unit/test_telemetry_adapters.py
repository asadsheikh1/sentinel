"""Telemetry adapters: recorded real responses, query building, injection, caps, errors."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from sentinel.telemetry import (
    InvalidQueryError,
    JaegerClient,
    Limits,
    LokiClient,
    PrometheusClient,
    Selector,
    TelemetryError,
    TelemetryTimeoutError,
    Template,
    TimeRange,
)
from sentinel.telemetry import loki as loki_mod
from sentinel.telemetry import prometheus as prom_mod
from sentinel.telemetry.fixtures import (
    FixtureMissingError,
    FixtureTelemetryAdapter,
    Mode,
    request_key,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry"
ERROR_TRACE = (FIXTURES / "trace_id.txt").read_text().strip()
WINDOW = TimeRange.last(20)
AVIRO = Selector(namespace="aviro")


@pytest.fixture
def replay() -> httpx.AsyncBaseTransport:
    return FixtureTelemetryAdapter(FIXTURES / "aviro.json", Mode.REPLAY).transport()


def mock(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ------------------------------------------------- recorded real responses


async def test_prometheus_request_rate_for_every_aviro_service(
    replay: httpx.AsyncBaseTransport,
) -> None:
    async with PrometheusClient("http://prom", transport=replay) as prom:
        result = await prom.range(Template.REQUEST_RATE, AVIRO, WINDOW)
    services = {s.labels["service_name"] for s in result.series}
    assert services == {"gateway", "inventory", "orders", "payments"}
    assert all(s.points and all(v >= 0 for _, v in s.points) for s in result.series)
    assert result.step_s >= 15
    assert not result.truncated


async def test_prometheus_error_ratio_sees_the_payments_fault(
    replay: httpx.AsyncBaseTransport,
) -> None:
    async with PrometheusClient("http://prom", transport=replay) as prom:
        result = await prom.range(Template.ERROR_RATIO, AVIRO, WINDOW)
    by_service = {s.labels["service_name"]: max(v for _, v in s.points) for s in result.series}
    assert by_service["orders"] > 0  # the 504s while payments was slowed down
    assert "payments" not in by_service  # payments itself never answered 5xx


async def test_loki_error_logs_are_returned_newest_first(replay: httpx.AsyncBaseTransport) -> None:
    async with LokiClient("http://loki", transport=replay) as loki:
        result = await loki.logs(Selector(service="orders"), WINDOW, min_severity=17, limit=20)
    assert result.lines
    assert all("dependency call failed" in line.line for line in result.lines)
    assert [line.ts for line in result.lines] == sorted((x.ts for x in result.lines), reverse=True)
    assert all(line.trace_id and len(line.trace_id) == 32 for line in result.lines)


async def test_loki_redacts_secrets_that_bypassed_the_gateway(
    replay: httpx.AsyncBaseTransport,
) -> None:
    async with LokiClient("http://loki", transport=replay) as loki:
        result = await loki.logs(Selector(service="fixture-secrets"), WINDOW, limit=20)
    text = "\n".join(line.line for line in result.lines)
    for secret in (
        "Pr0d-Passw0rd!",
        "eyJhbGciOiJIUzI1NiJ9",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_FixtureToken",
        "jane.doe@example.com",
        "4111 1111 1111 1111",
        "Zx8Qp2LmN4vR7tY1wK9sD3fG6hJ0aB5c",
    ):
        assert secret not in text
    for control in ("\x1b", "\x07", "\x00"):
        assert control not in text
    assert "4bf92f3577b34da6a3ce929d0e0e4736" in text  # trace ids survive
    assert result.redactions["url_credentials"] == 1
    assert sum(result.redactions.values()) >= 7


async def test_jaeger_finds_error_traces(replay: httpx.AsyncBaseTransport) -> None:
    async with JaegerClient("http://jaeger", transport=replay) as jaeger:
        result = await jaeger.find("orders", WINDOW, errors_only=True, limit=3)
    assert 1 <= len(result.traces) <= 3
    assert all(t.has_error for t in result.traces)
    assert all({"gateway", "orders", "payments"} <= set(t.services) for t in result.traces)


async def test_jaeger_trace_shows_where_the_time_went(replay: httpx.AsyncBaseTransport) -> None:
    async with JaegerClient("http://jaeger", transport=replay) as jaeger:
        trace = await jaeger.trace(ERROR_TRACE)
        services = await jaeger.services()
    assert trace is not None
    assert trace.trace_id == ERROR_TRACE
    assert trace.root is not None
    assert trace.root.name == "POST /api/orders"
    assert trace.root.duration_ms > 2500  # the client's 3 s timeout fired
    assert any(s.error and s.service == "orders" for s in trace.spans)
    assert {"gateway", "orders", "payments", "inventory"} <= set(services)


async def test_replay_never_touches_the_network(replay: httpx.AsyncBaseTransport) -> None:
    async with PrometheusClient("http://nowhere.invalid", transport=replay) as prom:
        with pytest.raises(FixtureMissingError):
            await prom.range(Template.LATENCY_P99, AVIRO, WINDOW)  # never recorded


# ------------------------------------------------------- query building


def test_queries_are_built_from_templates() -> None:
    sel = Selector(service="orders", environment="dev")
    assert prom_mod.build_query(Template.REQUEST_RATE, sel) == (
        "sum by (service_name) (rate(http_server_request_duration_seconds_count"
        '{deployment_environment_name="dev", service_name="orders"}[5m]))'
    )
    assert prom_mod.build_query(Template.SERIES, sel, "system_cpu_utilization_ratio").startswith(
        "system_cpu_utilization_ratio{"
    )
    assert loki_mod.build_query(sel, "time out", 17) == (
        '{deployment_environment_name="dev", service_name="orders"} |= "time out"'
        " | severity_number >= 17"
    )


@pytest.mark.parametrize(
    "hostile",
    [
        'orders"} or vector(1) or {x="',
        "orders\n} | json",
        "orders`",
        "a" * 201,
        "",
        "orders$(id)",
    ],
)
def test_selector_values_cannot_inject_query_syntax(hostile: str) -> None:
    with pytest.raises(InvalidQueryError):
        Selector(service=hostile)


def test_text_filter_is_escaped_not_executed() -> None:
    query = loki_mod.build_query(Selector(service="orders"), 'x" |~ ".*', None)
    assert query == '{service_name="orders"} |= "x\\" |~ \\".*"'


@pytest.mark.parametrize("bad", ["", "\x1b[31m", "x" * 201])
def test_bad_text_filters_are_rejected(bad: str) -> None:
    with pytest.raises(InvalidQueryError):
        loki_mod.build_query(Selector(service="orders"), bad, None)


def test_metric_names_and_parameters_are_validated() -> None:
    sel = Selector(service="orders")
    for bad in ("up or vector(1)", "1abc", "a b"):
        with pytest.raises(InvalidQueryError):
            prom_mod.build_query(Template.SERIES, sel, bad)
    with pytest.raises(InvalidQueryError):
        prom_mod.build_query(Template.REQUEST_RATE, sel, "up")
    with pytest.raises(InvalidQueryError):
        loki_mod.build_query(sel, None, 99)


def test_selector_and_window_rules() -> None:
    with pytest.raises(InvalidQueryError):
        Selector()
    assert Selector(integration_id=uuid.UUID(int=1)).labels() == {
        "sentinel_integration_id": "00000000-0000-0000-0000-000000000001"
    }
    now = datetime.now(UTC)
    with pytest.raises(InvalidQueryError):
        TimeRange(now, now)
    with pytest.raises(InvalidQueryError):
        TimeRange(now - timedelta(days=8), now)
    with pytest.raises(InvalidQueryError):
        TimeRange(datetime(2026, 1, 1), datetime(2026, 1, 2))  # naive


def test_step_keeps_series_under_the_point_cap() -> None:
    assert prom_mod.step_seconds(TimeRange.last(15), 600) == 15
    day = TimeRange.last(24 * 60)
    assert prom_mod.step_seconds(day, 600) == 144
    assert day.seconds / prom_mod.step_seconds(day, 600) <= 600


# ------------------------------------------------------------ caps & errors


def _prom_body(n_series: int, n_points: int) -> dict[str, Any]:
    return {
        "status": "success",
        "data": {
            "result": [
                {"metric": {"service_name": f"s{i}"}, "values": [[t, "1"] for t in range(n_points)]}
                for i in range(n_series)
            ]
        },
    }


async def test_series_and_point_caps() -> None:
    transport = mock(lambda r: httpx.Response(200, json=_prom_body(10, 50)))
    limits = Limits(max_series=3, max_points=20)
    async with PrometheusClient("http://p", limits, transport=transport) as prom:
        result = await prom.range(Template.REQUEST_RATE, AVIRO, WINDOW)
    assert result.truncated
    assert len(result.series) == 3
    assert all(len(s.points) == 20 for s in result.series)


async def test_response_byte_cap() -> None:
    transport = mock(lambda r: httpx.Response(200, content=b"x" * 5000))
    async with PrometheusClient(
        "http://p", Limits(max_response_bytes=1024), transport=transport
    ) as p:
        with pytest.raises(TelemetryError, match="larger than"):
            await p.range(Template.REQUEST_RATE, AVIRO, WINDOW)


async def test_log_line_and_count_caps() -> None:
    values = [[str(1_800_000_000_000_000_000 + i), "y" * 50] for i in range(30)]
    body = {
        "status": "success",
        "data": {"result": [{"stream": {"service_name": "x"}, "values": values}]},
    }
    transport = mock(lambda r: httpx.Response(200, json=body))
    async with LokiClient("http://l", Limits(max_line_chars=30), transport=transport) as loki:
        result = await loki.logs(Selector(service="x"), WINDOW, limit=10)
    assert result.truncated
    assert len(result.lines) == 10
    assert all(len(line.line) <= 30 for line in result.lines)


async def test_loki_drops_unknown_labels() -> None:
    body = {
        "status": "success",
        "data": {
            "result": [
                {
                    "stream": {"service_name": "x", "user_email": "a@b.co", "trace_id": "ab" * 16},
                    "values": [["1800000000000000000", "hi"]],
                }
            ]
        },
    }
    async with LokiClient(
        "http://l", transport=mock(lambda r: httpx.Response(200, json=body))
    ) as lk:
        result = await lk.logs(Selector(service="x"), WINDOW)
    assert result.lines[0].labels == {"service_name": "x", "trace_id": "ab" * 16}


async def test_span_attributes_are_cleaned() -> None:
    body = {
        "result": {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [{"key": "service.name", "value": {"stringValue": "api"}}]
                    },
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "ab" * 16,
                                    "spanId": "cd" * 8,
                                    "name": "GET /login",
                                    "startTimeUnixNano": "1800000000000000000",
                                    "endTimeUnixNano": "1800000000005000000",
                                    "status": {
                                        "code": 2,
                                        "message": "auth failed for bob@example.com",
                                    },
                                    "attributes": [
                                        {
                                            "key": "http.request.header.authorization",
                                            "value": {"stringValue": "Bearer zzzzzzzzzzzz"},
                                        },
                                        {
                                            "key": "db.statement",
                                            "value": {
                                                "stringValue": (
                                                    "SELECT * FROM u WHERE password='hunter22'"
                                                )
                                            },
                                        },
                                        {
                                            "key": "http.response.status_code",
                                            "value": {"intValue": "500"},
                                        },
                                    ],
                                }
                            ]
                        }
                    ],
                }
            ]
        }
    }
    async with JaegerClient(
        "http://j", transport=mock(lambda r: httpx.Response(200, json=body))
    ) as j:
        trace = await j.trace("ab" * 16)
    assert trace is not None
    span = trace.spans[0]
    assert span.error
    assert span.duration_ms == pytest.approx(5.0)
    dumped = json.dumps(span.attributes) + str(span.status_message)
    for leaked in ("zzzzzzzzzzzz", "hunter22", "bob@example.com"):
        assert leaked not in dumped
    assert span.attributes["http.response.status_code"] == 500


async def test_jaeger_404_means_nothing_found() -> None:
    transport = mock(lambda r: httpx.Response(404, json={"error": {"message": "No traces found"}}))
    async with JaegerClient("http://j", transport=transport) as j:
        assert await j.trace("ab" * 16) is None
        assert (await j.find("orders", WINDOW)).traces == []
        with pytest.raises(InvalidQueryError):
            await j.trace("not-a-trace-id")


async def test_backend_failures_become_telemetry_errors() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    async with PrometheusClient("http://p", transport=mock(boom)) as p:
        with pytest.raises(TelemetryError, match="unreachable"):
            await p.range(Template.REQUEST_RATE, AVIRO, WINDOW)
    async with PrometheusClient("http://p", transport=mock(slow)) as p:
        with pytest.raises(TelemetryTimeoutError):
            await p.range(Template.REQUEST_RATE, AVIRO, WINDOW)
    async with LokiClient("http://l", transport=mock(lambda r: httpx.Response(503))) as lk:
        with pytest.raises(TelemetryError, match="503"):
            await lk.logs(Selector(service="x"), WINDOW)
    bad_query = {"status": "error", "error": "parse error"}
    async with PrometheusClient(
        "http://p", transport=mock(lambda r: httpx.Response(400, json=bad_query))
    ) as p:
        with pytest.raises(TelemetryError, match="parse error"):
            await p.range(Template.REQUEST_RATE, AVIRO, WINDOW)


# ------------------------------------------------------------- record mode


@pytest.mark.parametrize("name", ["cassette.json", "cassette.json.gz"])
async def test_record_then_replay_round_trip(tmp_path: Path, name: str) -> None:
    upstream = mock(lambda r: httpx.Response(200, json=_prom_body(1, 3)))
    path = tmp_path / name
    recorder = FixtureTelemetryAdapter(path, Mode.RECORD)
    async with PrometheusClient("http://p", transport=recorder.transport(upstream)) as p:
        first = await p.range(Template.REQUEST_RATE, AVIRO, WINDOW)
    recorder.save()

    later = TimeRange.last(20, now=datetime.now(UTC) + timedelta(hours=3))  # different times
    player = FixtureTelemetryAdapter(path, Mode.REPLAY)
    async with PrometheusClient("http://p", transport=player.transport()) as p:
        second = await p.range(Template.REQUEST_RATE, AVIRO, later)
    assert [s.labels for s in second.series] == [s.labels for s in first.series]


async def test_gzip_fixture_is_compressed_and_deterministic(tmp_path: Path) -> None:
    upstream = mock(lambda r: httpx.Response(200, json=_prom_body(1, 3)))
    blobs = []
    for name in ("a.json.gz", "b.json.gz"):
        recorder = FixtureTelemetryAdapter(tmp_path / name, Mode.RECORD)
        async with PrometheusClient("http://p", transport=recorder.transport(upstream)) as p:
            await p.range(Template.REQUEST_RATE, AVIRO, WINDOW)
        recorder.save()
        blobs.append((tmp_path / name).read_bytes())
    assert blobs[0][:2] == b"\x1f\x8b"  # gzip magic
    assert blobs[0] == blobs[1]


def test_request_key_ignores_time_but_not_the_query() -> None:
    a = httpx.Request("GET", "http://p/q", params={"query": "up", "start": "1", "end": "2"})
    b = httpx.Request("GET", "http://p/q", params={"query": "up", "start": "9", "end": "10"})
    c = httpx.Request("GET", "http://p/q", params={"query": "down", "start": "1", "end": "2"})
    assert request_key(a) == request_key(b) != request_key(c)


def test_missing_fixture_file() -> None:
    with pytest.raises(FixtureMissingError):
        FixtureTelemetryAdapter(Path("/nonexistent/x.json"), Mode.REPLAY)
