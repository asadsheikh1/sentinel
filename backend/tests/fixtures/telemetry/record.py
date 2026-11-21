"""Re-record ``aviro.json`` from a live stack with AVIRO traffic (see the step 7 report).

    uv run python tests/fixtures/telemetry/record.py <trace_id_with_error>

The unit tests replay the file, so they run without any backend.
"""

import asyncio
import sys
from pathlib import Path

from sentinel.telemetry import JaegerClient, LokiClient, PrometheusClient, Selector, Template
from sentinel.telemetry.base import TimeRange
from sentinel.telemetry.fixtures import FixtureTelemetryAdapter, Mode

HERE = Path(__file__).parent
CALLS = HERE / "calls.py"


async def main(error_trace_id: str) -> None:
    fixtures = FixtureTelemetryAdapter(HERE / "aviro.json", Mode.RECORD)
    transport = fixtures.transport()
    window = TimeRange.last(20)
    aviro = Selector(namespace="aviro")
    async with (
        PrometheusClient("http://127.0.0.1:9090", transport=transport) as prom,
        LokiClient("http://127.0.0.1:3100", transport=transport) as loki,
        JaegerClient("http://127.0.0.1:16686", transport=transport) as jaeger,
    ):
        for template in (Template.REQUEST_RATE, Template.ERROR_RATIO, Template.LATENCY_P95):
            await prom.range(template, aviro, window)
        await loki.logs(Selector(service="orders"), window, min_severity=17, limit=20)
        await loki.logs(Selector(service="fixture-secrets"), window, limit=20)
        await jaeger.find("orders", window, errors_only=True, limit=3)
        await jaeger.trace(error_trace_id)
        await jaeger.services()
    fixtures.save()
    (HERE / "trace_id.txt").write_text(error_trace_id + "\n")
    print(f"recorded {HERE / 'aviro.json'}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
