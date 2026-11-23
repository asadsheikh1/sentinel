"""An in-memory Loki for the error poller: /loki/api/v1/query_range, forward, with limit.

It knows the poller's two queries: the label filter (``severity_number >= 17 or
exception_type != ""``) and the line regex (a traceback, or a JSON level).
``fail_on`` makes the n-th request fail, to test restart safety.
"""

import re
from dataclasses import dataclass, field
from typing import Any

import httpx

_LINE = re.compile(
    r'Traceback \(most recent call last\)|"(?:level|levelname|severity)": ?"(?i:error|critical|fatal)"'  # noqa: E501
)


@dataclass
class Entry:
    ns: int
    line: str
    labels: dict[str, str]


@dataclass
class FakeLoki:
    entries: list[Entry] = field(default_factory=list)
    requests: int = 0
    fail_on: int | None = None
    max_limit: int = 5000

    def add(self, ns: int, line: str, **labels: str) -> None:
        labels.setdefault("sentinel_integration_id", "00000000-0000-0000-0000-000000000000")
        self.entries.append(Entry(ns, line, labels))

    def _matches(self, query: str, e: Entry) -> bool:
        if "|~" in query:
            return bool(_LINE.search(e.line))
        try:
            sev = int(e.labels.get("severity_number", "0"))
        except ValueError:
            sev = 0
        return sev >= 17 or bool(e.labels.get("exception_type"))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self.fail_on is not None and self.requests == self.fail_on:
            return httpx.Response(503, json={"status": "error"})
        assert request.url.path == "/loki/api/v1/query_range"
        p = request.url.params
        assert p["direction"] == "forward"
        start, end, limit = int(p["start"]), int(p["end"]), min(int(p["limit"]), self.max_limit)
        hits = sorted(
            (e for e in self.entries if start <= e.ns < end and self._matches(p["query"], e)),
            key=lambda e: e.ns,
        )[:limit]
        streams: dict[tuple[tuple[str, str], ...], list[list[str]]] = {}
        for e in hits:
            streams.setdefault(tuple(sorted(e.labels.items())), []).append([str(e.ns), e.line])
        body: dict[str, Any] = {
            "status": "success",
            "data": {
                "resultType": "streams",
                "result": [{"stream": dict(k), "values": v} for k, v in streams.items()],
            },
        }
        return httpx.Response(200, json=body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)
