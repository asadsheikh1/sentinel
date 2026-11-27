"""Adapters against the live stores, with data that came in through the gateway (Step 7).

Needs the full stack: `make stack-up-fast`, then `make test-gateway`.
"""

import secrets
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx2 as httpx
import pytest

from sentinel.telemetry import (
    JaegerClient,
    LokiClient,
    PrometheusClient,
    Selector,
    Template,
    TimeRange,
)

from .test_ingest_e2e import Admin, logs_body, metrics_body, send, traces_body

pytestmark = pytest.mark.gateway

PROM = "http://127.0.0.1:9090"
LOKI = "http://127.0.0.1:3100"
JAEGER = "http://127.0.0.1:16686"


async def eventually(fetch: Callable[[], Any], wait_s: float = 45, every: float = 1.5) -> Any:
    import asyncio

    end = time.monotonic() + wait_s
    last: Any = None
    while time.monotonic() < end:
        last = await fetch()
        if last:
            return last
        await asyncio.sleep(every)
    pytest.fail(f"condition not met after {wait_s}s; last={last!r}")


def window() -> TimeRange:
    """Computed per query: a window fixed before sending would end before the new data."""
    return TimeRange.last(30)


async def test_adapters_read_back_gateway_data_by_integration(
    http: httpx.Client, admin: Admin
) -> None:
    iid, token = admin.create()
    other_iid, other_token = admin.create()
    run = uuid.uuid4().hex[:10]
    service = f"adapt-{run}"
    trace_id = secrets.token_hex(16)
    resource = {"service.name": service, "service.namespace": "e2e"}

    line = f"checkout failed run {run} password=hunter2hunter2"
    for path, body, tok in (
        ("/v1/logs", logs_body(resource, line, severity="ERROR"), token),
        ("/v1/metrics", metrics_body(resource, "e2e.adapter", {"run": run}), token),
        ("/v1/traces", traces_body(resource, trace_id, f"op-{run}", error=True), token),
        ("/v1/logs", logs_body({"service.name": service}, f"other {run}"), other_token),
    ):
        assert send(http, path, body, tok).status_code == 200

    mine = Selector(service=service, integration_id=uuid.UUID(iid))
    theirs = Selector(service=service, integration_id=uuid.UUID(other_iid))

    async with LokiClient(LOKI) as loki:

        async def my_logs() -> Any:
            return (await loki.logs(mine, window(), min_severity=17)).lines

        lines = await eventually(my_logs)
        # The gateway already redacted it before storage; the adapter leaves that alone.
        assert len(lines) == 1
        assert lines[0].line.startswith(f"checkout failed run {run} password=[REDACTED")
        assert "hunter2hunter2" not in lines[0].line
        assert lines[0].labels["sentinel_integration_id"] == iid

        async def their_logs() -> Any:
            return (await loki.logs(theirs, window())).lines

        other = await eventually(their_logs)
        assert [x.line for x in other] == [f"other {run}"]  # filters don't leak across integrations

    async with PrometheusClient(PROM) as prom:

        async def series() -> Any:
            result = await prom.range(Template.SERIES, mine, window(), metric="e2e_adapter_ratio")
            return result.series

        found = await eventually(series)
        assert found[0].labels["run"] == run
        assert found[0].points[-1][1] == 7.0

    async with JaegerClient(JAEGER) as jaeger:

        async def trace() -> Any:
            return await jaeger.trace(trace_id)

        got = await eventually(trace)
        assert got.spans[0].name == f"op-{run}"
        assert got.spans[0].attributes == {}
        assert service in await jaeger.services()


async def test_template_queries_run_against_real_prometheus() -> None:
    """Every template must be valid PromQL (an empty result is fine, an error is not)."""
    async with PrometheusClient(PROM) as prom:
        for template in Template:
            if template is Template.SERIES:
                result = await prom.range(template, Selector(service="x"), window(), metric="up")
            else:
                result = await prom.range(template, Selector(namespace="aviro"), window())
            assert result.query
