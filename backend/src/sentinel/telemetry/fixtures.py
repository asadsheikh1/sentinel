"""Record and replay telemetry backend responses.

``FixtureTelemetryAdapter`` produces an httpx transport for the adapters:

* ``record``: pass requests to the real backend and save every response;
* ``replay``: answer from the saved file and never touch the network.

Requests are matched on method, path and query parameters, ignoring time
parameters, so a replay at a different time still finds its responses. Recorded
files are what the unit tests and (later) evaluation scenarios replay. A path
ending in ``.gz`` is read and written gzip-compressed.
"""

import gzip
import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx

# Parameters that change from run to run and are ignored when matching.
TIME_PARAMS = frozenset(
    {"start", "end", "time", "step", "query.start_time_min", "query.start_time_max"}
)


class Mode(StrEnum):
    RECORD = "record"
    REPLAY = "replay"


class FixtureMissingError(LookupError):
    pass


def request_key(request: httpx.Request) -> str:
    params = sorted((k, v) for k, v in request.url.params.multi_items() if k not in TIME_PARAMS)
    raw = json.dumps([request.method, request.url.path, params])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class FixtureTelemetryAdapter:
    def __init__(self, path: Path, mode: Mode) -> None:
        self.path = path
        self.mode = mode
        self._entries: dict[str, dict[str, Any]] = {}
        if mode is Mode.REPLAY:
            if not path.exists():
                raise FixtureMissingError(f"no fixture file at {path}")
            raw = path.read_bytes()
            if path.suffix == ".gz":
                raw = gzip.decompress(raw)
            self._entries = json.loads(raw)["responses"]

    def transport(
        self, upstream: httpx.AsyncBaseTransport | None = None
    ) -> httpx.AsyncBaseTransport:
        if self.mode is Mode.REPLAY:
            return httpx.MockTransport(self._replay)
        return _RecordingTransport(self, upstream or httpx.AsyncHTTPTransport())

    def _replay(self, request: httpx.Request) -> httpx.Response:
        entry = self._entries.get(request_key(request))
        if entry is None:
            raise FixtureMissingError(
                f"not recorded: {request.method} {request.url.path} {request.url.params}"
            )
        return httpx.Response(
            entry["status"],
            content=entry["body"].encode(),
            headers={"content-type": "application/json"},
        )

    def remember(self, request: httpx.Request, status: int, body: bytes) -> None:
        self._entries[request_key(request)] = {
            "method": request.method,
            "path": request.url.path,
            "params": {k: v for k, v in request.url.params.items() if k not in TIME_PARAMS},
            "status": status,
            "body": body.decode(),
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"responses": self._entries}, indent=2, sort_keys=True) + "\n"
        if self.path.suffix == ".gz":  # mtime=0: the same responses give the same bytes
            self.path.write_bytes(gzip.compress(text.encode(), mtime=0))
        else:
            self.path.write_text(text)


class _RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(
        self, fixtures: FixtureTelemetryAdapter, upstream: httpx.AsyncBaseTransport
    ) -> None:
        self.fixtures = fixtures
        self.upstream = upstream

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.upstream.handle_async_request(request)
        body = await response.aread()
        self.fixtures.remember(request, response.status_code, body)
        # `body` is already decompressed: drop the headers that describe the wire encoding,
        # or the client would try to decompress it a second time.
        headers = {
            k: v
            for k, v in response.headers.items()
            if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
        }
        return httpx.Response(response.status_code, content=body, headers=headers)

    async def aclose(self) -> None:
        await self.upstream.aclose()
