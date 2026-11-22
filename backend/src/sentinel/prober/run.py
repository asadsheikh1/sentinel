"""The prober process: fetch checks, run them on schedule, send results.

It talks to SENTINEL only through the ingest endpoint, with its own ingest
token (read from a file the API provisions):

* ``GET  /api/v1/prober/checks`` — what to check (refreshed every 30 s);
* ``POST /v1/metrics`` and ``/v1/logs`` — the results, as OTLP.

Its allowlist (development only) comes from its own environment, never from the
server, so a compromised API can't widen what the guard lets through.
"""

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import structlog

from sentinel.config import Settings
from sentinel.prober.checks import CheckSpec, Result, run_check
from sentinel.prober.otlp import logs_payload, metrics_payload
from sentinel.prober.scheduler import Schedule
from sentinel.prober.ssrf_guard import Guard

log = structlog.get_logger("sentinel.prober")
FLUSH_INTERVAL_S = 2.0
MAX_BACKLOG = 2000  # results kept while SENTINEL is unreachable (about 20 min of 50 checks)


class TokenFile:
    """The prober's ingest token, re-read when SENTINEL rejects it (rotation)."""

    def __init__(self, path: str | None) -> None:
        self.path = Path(path) if path else None
        self._token: str | None = None

    def get(self, *, reload: bool = False) -> str | None:
        if self._token is None or reload:
            if self.path is None or not self.path.exists():
                self._token = None
            else:
                self._token = self.path.read_text(encoding="utf-8").strip() or None
        return self._token


def parse_checks(payload: dict[str, Any]) -> list[CheckSpec]:
    return [
        CheckSpec(
            id=str(c["id"]),
            name=str(c["name"]),
            kind=c["kind"],
            target=str(c["target"]),
            interval_s=int(c["interval_s"]),
            timeout_s=float(c["timeout_s"]),
            integration_id=str(c["integration_id"]),
            assertions=dict(c.get("assertions") or {}),
        )
        for c in payload.get("checks", [])
    ]


class Prober:
    def __init__(self, settings: Settings, guard: Guard | None = None) -> None:
        self.settings = settings
        self.guard = guard or Guard(allow_hosts=frozenset(settings.prober_allow_hosts))
        self.token = TokenFile(settings.prober_token_file)
        self.schedule = Schedule()
        self.results: list[Result] = []
        self.sentinel = httpx.AsyncClient(
            base_url=settings.prober_endpoint, timeout=10.0, trust_env=False
        )
        self._sem = asyncio.Semaphore(settings.prober_concurrency)
        self._running: set[asyncio.Task[None]] = set()

    async def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response | None:
        for attempt in (0, 1):
            token = self.token.get(reload=attempt == 1)
            if token is None:
                log.warning("prober_token_missing", path=str(self.token.path))
                return None
            response = await self.sentinel.request(
                method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs
            )
            if response.status_code != 401:
                return response
        log.warning("prober_token_rejected")
        return None

    async def refresh(self) -> int:
        response = await self._call("GET", "/api/v1/prober/checks")
        if response is None or response.status_code != 200:
            return -1
        checks = parse_checks(response.json())
        self.schedule.update(checks, time.monotonic())
        return len(checks)

    async def flush(self) -> bool:
        """Send buffered results. On failure they are kept (up to MAX_BACKLOG)."""
        batch, self.results = self.results, []
        if not batch:
            return True
        try:
            sent = await self._call("POST", "/v1/metrics", json=metrics_payload(batch))
            if sent is not None and sent.is_success and any(not r.success for r in batch):
                await self._call("POST", "/v1/logs", json=logs_payload(batch))
        except httpx.HTTPError:
            sent = None
        if sent is None or not sent.is_success:
            self.results = (batch + self.results)[-MAX_BACKLOG:]
            return False
        return True

    async def _run_one(self, spec: CheckSpec) -> None:
        async with self._sem:
            result = await run_check(spec, self.guard)
        self.results.append(result)
        if not result.success:
            log.info("probe_failed", check=spec.name, error=result.error)

    def tick(self) -> None:
        for spec in self.schedule.due(time.monotonic()):
            task = asyncio.create_task(self._run_one(spec))
            self._running.add(task)
            task.add_done_callback(self._running.discard)

    async def run_forever(self) -> None:
        log.info("prober_started", endpoint=self.settings.prober_endpoint)
        next_refresh = next_flush = 0.0
        while True:
            now = time.monotonic()
            if now >= next_refresh:
                try:
                    count = await self.refresh()
                    log.debug("prober_checks_loaded", count=count)
                except httpx.HTTPError as exc:
                    log.warning("prober_refresh_failed", error=type(exc).__name__)
                next_refresh = now + self.settings.prober_config_refresh_s
            self.tick()
            if now >= next_flush:
                if not await self.flush():
                    log.warning("prober_export_failed", backlog=len(self.results))
                next_flush = now + FLUSH_INTERVAL_S
            await asyncio.sleep(self.schedule.sleep_for(time.monotonic(), maximum=0.5))


def main(settings: Settings) -> int:
    asyncio.run(Prober(settings).run_forever())
    return 0  # pragma: no cover - runs until stopped
