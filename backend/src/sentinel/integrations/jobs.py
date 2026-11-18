"""Background loops run inside the API process (the gate's last-seen data lives there)."""

import asyncio
from collections.abc import Awaitable, Callable

import structlog

from sentinel.db.engine import session_scope
from sentinel.integrations.entities import Query, discover, prometheus_query
from sentinel.integrations.gate import IngestGate

log = structlog.get_logger("sentinel.integrations")


async def _forever(name: str, interval_s: float, job: Callable[[], Awaitable[object]]) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            await job()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a failing job must never take the API down
            log.warning("background_job_failed", job=name, error=type(exc).__name__)


async def health_pass(gate: IngestGate) -> None:
    await gate.flush()
    await gate.evaluate()


async def discovery_pass(gate: IngestGate, query: Query) -> None:
    async with session_scope(gate.session_factory) as db:
        _, gate.series_counts = await discover(db, query)


def start(gate: IngestGate) -> list[asyncio.Task[None]]:
    settings = gate.settings
    query = prometheus_query(settings.prometheus_url)
    return [
        asyncio.create_task(
            _forever(
                "integration_health",
                settings.integration_flush_interval_s,
                lambda: health_pass(gate),
            )
        ),
        asyncio.create_task(
            _forever(
                "entity_discovery",
                settings.entity_discovery_interval_s,
                lambda: discovery_pass(gate, query),
            )
        ),
    ]


async def stop(tasks: list[asyncio.Task[None]], gate: IngestGate) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    try:
        await gate.flush()  # keep the last seconds of last-seen data
    except Exception as exc:
        log.warning("final_flush_failed", error=type(exc).__name__)
