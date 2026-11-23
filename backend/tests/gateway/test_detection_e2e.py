"""Detection end to end (Step 10): telemetry → rule pack → Alertmanager → signal.

A made-up service sends OTLP HTTP-server metrics through the gateway, like an
instrumented app: healthy first, then one request in five fails. The service
rule pack must turn that into a ``ServiceHighErrorRatio`` signal within 3
minutes of the fault (the S01 criterion), attributed to the service.

Needs `make stack-up-fast` (Prometheus, Alertmanager and the API with rule packs).
"""

import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest

from .test_ingest_e2e import INGEST, Admin, eventually

pytestmark = pytest.mark.gateway

BUCKETS = [0.1, 0.25, 0.5, 1.0]


def _histogram(
    service: str, ns: str, counts: dict[str, int], ts: int, start: int
) -> dict[str, Any]:
    """Cumulative http.server.request.duration points, one per status code."""
    points = []
    for status, count in counts.items():
        points.append(
            {
                "attributes": [
                    {"key": "http.response.status_code", "value": {"intValue": status}},
                    {"key": "http.request.method", "value": {"stringValue": "POST"}},
                    {"key": "http.route", "value": {"stringValue": "/api/orders"}},
                ],
                "startTimeUnixNano": str(start),
                "timeUnixNano": str(ts),
                "count": str(count),
                "sum": count * 0.05,
                "bucketCounts": [str(count), "0", "0", "0", "0"],
                "explicitBounds": BUCKETS,
            }
        )
    metric = {
        "name": "http.server.request.duration",
        "unit": "s",
        "histogram": {"aggregationTemporality": 2, "dataPoints": points},
    }
    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": service}},
                        {"key": "service.namespace", "value": {"stringValue": ns}},
                        {"key": "deployment.environment.name", "value": {"stringValue": "ci"}},
                    ]
                },
                "scopeMetrics": [{"metrics": [metric]}],
            }
        ]
    }


class FakeService(threading.Thread):
    """Sends 10 req/s worth of counters every 5 s; `fail()` makes 20% of them 500s."""

    def __init__(self, token: str, service: str, ns: str) -> None:
        super().__init__(daemon=True)
        self.token, self.service, self.ns = token, service, ns
        self.ok = self.errors = 0
        self.failing = False
        self.stop = threading.Event()
        self.start_ns = time.time_ns()

    def run(self) -> None:
        with httpx.Client(timeout=10) as client:
            while not self.stop.is_set():
                if self.failing:
                    self.ok += 40
                    self.errors += 10
                else:
                    self.ok += 50
                body = _histogram(
                    self.service,
                    self.ns,
                    {"200": self.ok, "500": self.errors},
                    time.time_ns(),
                    self.start_ns,
                )
                client.post(
                    f"{INGEST}/v1/metrics",
                    json=body,
                    headers={"Authorization": f"Bearer {self.token}"},
                )
                self.stop.wait(5)


ALERTMANAGER = "http://127.0.0.1:9093"


def test_alertmanager_delivers_to_sentinel(admin: Admin) -> None:
    """The last hop on its own: an alert posted to Alertmanager becomes a signal,
    and resolving it resolves the signal. Fails fast (and says where) if the
    webhook, its key file, or the network between them is broken."""
    service = f"am-{uuid.uuid4().hex[:6]}"
    alert = {
        "labels": {
            "alertname": "SentinelDeliveryCheck",
            "severity": "info",
            "pack": "e2e",
            "entity_type": "service",
            "service_name": service,
        },
        "annotations": {"summary": f"{service}: delivery check"},
    }
    with httpx.Client(base_url=ALERTMANAGER, timeout=10) as am:
        assert am.post("/api/v2/alerts", json=[alert]).status_code == 200

        def signals() -> list[dict[str, Any]]:
            return admin.get(f"/api/v1/signals?service={service}&kind=prom_alert")

        found = eventually(signals, timeout=60, every=2)
        assert found[0]["status"] == "firing"
        assert found[0]["title"] == f"{service}: delivery check"

        ended = {**alert, "endsAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        assert am.post("/api/v2/alerts", json=[ended]).status_code == 200
        # A resolved notification waits for the group interval (1 min).
        eventually(lambda: [s for s in signals() if s["status"] == "resolved"], timeout=120)


def test_a_5xx_burst_becomes_a_service_signal_within_3_minutes(admin: Admin) -> None:
    _, token = admin.create(kind="otel_app", expected_signals=["metrics"])
    service = f"checkout-{uuid.uuid4().hex[:6]}"
    fake = FakeService(token, service, "ci")
    fake.start()
    try:
        time.sleep(75)  # healthy traffic first: no alert
        quiet = admin.get(f"/api/v1/signals?service={service}")
        assert quiet == [], quiet

        fake.failing = True
        started = time.time()

        def signal() -> list[dict[str, Any]]:
            return [
                s
                for s in admin.get(f"/api/v1/signals?service={service}&kind=prom_alert")
                if s["labels"].get("alertname") == "ServiceHighErrorRatio"
            ]

        found = eventually(signal, timeout=180, every=5)
        took = time.time() - started
        print(f"signal {took:.0f}s after the fault")  # published in the step report
        assert took <= 180
        s = found[0]
        assert s["severity"] == "critical"
        assert s["status"] == "firing"
        assert s["title"].startswith(f"{service}: 20%")

        # Recovery: errors stop, the signal resolves (Alertmanager sends "resolved").
        fake.failing = False
        eventually(lambda: all(x["status"] == "resolved" for x in signal()), timeout=300, every=10)
    finally:
        fake.stop.set()
        fake.join(timeout=10)


def _error_logs(service: str, n: int) -> dict[str, Any]:
    """`n` OTLP error records of one KeyError, each from a different request (Step 11)."""
    stack = (
        Path(__file__).resolve().parents[1] / "fixtures" / "tracebacks" / "aviro_s01_keyerror.txt"
    ).read_text()
    now = time.time_ns()
    records = [
        {
            "timeUnixNano": str(now + i * 1000),
            "severityNumber": 17,
            "severityText": "ERROR",
            "body": {"stringValue": "unhandled exception"},
            "traceId": uuid.uuid4().hex,
            "spanId": uuid.uuid4().hex[:16],
            "attributes": [
                {"key": k, "value": {"stringValue": v}}
                for k, v in {
                    "exception.type": "KeyError",
                    "exception.message": "gift_message",
                    "exception.stacktrace": stack,
                    "code.file.path": "/app/.venv/lib/python3.12/site-packages/aviro/common/service.py",  # noqa: E501
                    "code.function.name": "faults_and_errors",
                    "order_id": str(uuid.uuid4()),
                }.items()
            ],
        }
        for i in range(n)
    ]
    resource = {
        "service.name": service,
        "service.namespace": "ci",
        "deployment.environment.name": "ci",
    }
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {"key": k, "value": {"stringValue": v}} for k, v in resource.items()
                    ]
                },
                "scopeLogs": [{"scope": {"name": "aviro.orders"}, "logRecords": records}],
            }
        ]
    }


def test_exception_logs_become_one_error_group_and_a_signal(admin: Admin) -> None:
    """Step 11 live: OTLP exception logs → Loki → poller → one group, one signal."""
    _, token = admin.create(kind="otel_app", expected_signals=["logs"])
    service = f"orders-{uuid.uuid4().hex[:6]}"
    with httpx.Client(timeout=10) as http:
        for _ in range(3):  # three batches, 30 failing requests in all
            r = http.post(
                f"{INGEST}/v1/logs",
                json=_error_logs(service, 10),
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200, r.text
    groups = eventually(
        lambda: [
            g
            for g in admin.get(f"/api/v1/error-groups?service={service}")
            if g["total_count"] >= 30
        ],
        timeout=90,
        every=3,
    )
    assert len(groups) == 1
    assert groups[0]["exception_type"] == "KeyError"
    assert groups[0]["top_frame"] == "aviro/orders/app.py:create_order"
    assert groups[0]["total_count"] == 30
    signals = admin.get(f"/api/v1/signals?service={service}&kind=error_group_new")
    assert len(signals) == 1
    assert signals[0]["status"] == "firing"


def test_deployments_and_github_events_are_ingested_live(admin: Admin) -> None:
    import json

    from sentinel.security.webhooks import sign

    r = admin.client.post(
        "/api/v1/api-keys",
        json={"name": f"ci-{uuid.uuid4().hex[:6]}", "scopes": ["deployments:write"]},
        headers=admin.h,
    )
    key = r.json()["secret"]
    service = f"svc-{uuid.uuid4().hex[:6]}"
    body = {"service": service, "version": "v1", "commit_sha": "c" * 40}
    with httpx.Client(base_url=str(admin.client.base_url), timeout=10) as machine:
        headers = {"X-Sentinel-Key": key, "Idempotency-Key": f"live-{service}"}
        assert (
            machine.post("/api/v1/ingest/deployments", json=body, headers=headers).status_code
            == 201
        )
        assert (
            machine.post("/api/v1/ingest/deployments", json=body, headers=headers).status_code
            == 200
        )
        raw = json.dumps({"zen": "Design for failure."}).encode()
        r = machine.post(
            "/api/v1/ingest/github",
            content=raw,
            headers={
                "X-GitHub-Event": "ping",
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-Hub-Signature-256": sign("dev-only-github-webhook-secret", raw),
            },
        )
        assert r.status_code == 202, r.text
    assert [d["version"] for d in admin.get(f"/api/v1/deployments?service={service}")] == ["v1"]
