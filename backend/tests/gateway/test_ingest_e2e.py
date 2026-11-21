"""End-to-end through Caddy -> gateway Collector -> stores (Step 6 acceptance).

Needs the whole stack with short intervals: `make stack-up-fast`, then `make test-gateway`.
"""

import os
import secrets
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx2 as httpx
import pytest

pytestmark = pytest.mark.gateway

API = os.environ.get("SENTINEL_TEST_API_URL", "http://127.0.0.1:8000")
INGEST = os.environ.get("SENTINEL_TEST_INGEST_URL", "http://127.0.0.1:4319")
PROM = "http://127.0.0.1:9090"
LOKI = "http://127.0.0.1:3100"
JAEGER = "http://127.0.0.1:16686"
PASSWORD = "gateway-e2e-password-1"


def eventually(fetch: Callable[[], Any], timeout: float = 45, every: float = 1.5) -> Any:
    end = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < end:
        try:
            last = fetch()
            if last:
                return last
        except (httpx.HTTPError, KeyError, IndexError):
            pass
        time.sleep(every)
    pytest.fail(f"condition not met after {timeout}s; last={last!r}")


def _attrs(values: dict[str, str]) -> list[dict[str, Any]]:
    return [{"key": k, "value": {"stringValue": v}} for k, v in values.items()]


def metrics_body(resource: dict[str, str], name: str, point: dict[str, str]) -> dict[str, Any]:
    dp = {"timeUnixNano": str(time.time_ns()), "asDouble": 7.0, "attributes": _attrs(point)}
    metric = {"name": name, "unit": "1", "gauge": {"dataPoints": [dp]}}
    return {
        "resourceMetrics": [
            {"resource": {"attributes": _attrs(resource)}, "scopeMetrics": [{"metrics": [metric]}]}
        ]
    }


def logs_body(
    resource: dict[str, str], body: str, severity: str = "INFO", attrs: dict[str, str] | None = None
) -> dict[str, Any]:
    record = {
        "timeUnixNano": str(time.time_ns()),
        "severityText": severity,
        "body": {"stringValue": body},
        "attributes": _attrs(attrs or {}),
    }
    return {
        "resourceLogs": [
            {"resource": {"attributes": _attrs(resource)}, "scopeLogs": [{"logRecords": [record]}]}
        ]
    }


def traces_body(resource: dict[str, str], trace_id: str, name: str) -> dict[str, Any]:
    now = time.time_ns()
    span = {
        "traceId": trace_id,
        "spanId": secrets.token_hex(8),
        "name": name,
        "kind": 2,
        "startTimeUnixNano": str(now),
        "endTimeUnixNano": str(now + 1_000_000),
    }
    return {
        "resourceSpans": [
            {"resource": {"attributes": _attrs(resource)}, "scopeSpans": [{"spans": [span]}]}
        ]
    }


class Admin:
    def __init__(self, client: httpx.Client, csrf: str) -> None:
        self.client = client
        self.h = {"X-CSRF-Token": csrf}

    def create(self, **body: Any) -> tuple[str, str]:
        payload = {"name": f"e2e-{uuid.uuid4().hex[:10]}", "kind": "otel_app", **body}
        r = self.client.post("/api/v1/integrations", json=payload, headers=self.h)
        assert r.status_code == 201, r.text
        return r.json()["integration"]["id"], r.json()["ingest_token"]

    def status(self, iid: str) -> dict[str, Any]:
        return self.client.get(f"/api/v1/integrations/{iid}/status").json()  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def admin() -> Iterator[Admin]:
    email = f"e2e-{uuid.uuid4().hex[:8]}@example.com"
    subprocess.run(
        [sys.executable, "-m", "sentinel.cli", "create-admin", "--email", email, "--name", "E2E",
         "--password-stdin"],
        input=f"{PASSWORD}\n",
        text=True,
        check=True,
        capture_output=True,
    )  # fmt: skip
    with httpx.Client(base_url=API, timeout=20) as client:
        r = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        assert r.status_code == 200, r.text
        yield Admin(client, r.json()["csrf_token"])


@pytest.fixture(scope="module")
def http() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=20) as client:
        yield client


def send(http: httpx.Client, path: str, body: dict[str, Any], token: str | None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return http.post(f"{INGEST}{path}", json=body, headers=headers)


def loki_streams(http: httpx.Client, selector: str) -> list[dict[str, Any]]:
    params = {
        "query": selector,
        "start": str(time.time_ns() - 600 * 10**9),
        "end": str(time.time_ns() + 60 * 10**9),
        "limit": "20",
    }
    response = http.get(f"{LOKI}/loki/api/v1/query_range", params=params)
    return response.json()["data"]["result"]  # type: ignore[no-any-return]


def prom_series(http: httpx.Client, query: str) -> list[dict[str, Any]]:
    response = http.get(f"{PROM}/api/v1/query", params={"query": query})
    return response.json()["data"]["result"]  # type: ignore[no-any-return]


# ---------------------------------------------------------------- the door


def test_requests_without_a_valid_token_are_rejected(http: httpx.Client) -> None:
    body = logs_body({"service.name": "intruder"}, "should never arrive")
    assert send(http, "/v1/logs", body, None).status_code == 401
    assert send(http, "/v1/logs", body, "nope").status_code == 401
    assert send(http, "/v1/logs", body, "sit_aaaaaaaa_" + "x" * 43).status_code == 401
    time.sleep(2)
    assert loki_streams(http, '{service_name="intruder"}') == []


def test_only_otlp_paths_are_exposed(http: httpx.Client, admin: Admin) -> None:
    _, token = admin.create()
    auth = {"Authorization": f"Bearer {token}"}
    assert http.get(f"{INGEST}/v1/logs", headers=auth).status_code == 404
    assert http.post(f"{INGEST}/internal/ingest-auth", headers=auth).status_code == 404
    assert http.get(f"{INGEST}/api/v1/users", headers=auth).status_code == 404


def test_only_agent_files_and_config_are_served_besides_otlp(
    http: httpx.Client, admin: Admin
) -> None:
    """Step 8: the ingest endpoint also serves the installer and each agent's config."""
    iid, token = admin.create(kind="linux_host")
    other_iid, _ = admin.create(kind="linux_host")
    auth = {"Authorization": f"Bearer {token}"}
    install = http.get(f"{INGEST}/agent/install.sh")
    assert install.status_code == 200
    assert install.text.startswith("#!/usr/bin/env bash")
    config = http.get(f"{INGEST}/api/v1/integrations/{iid}/agent-config", headers=auth)
    assert config.status_code == 200
    assert config.headers["x-sentinel-config-hash"]
    # Another integration's config, other agent paths, and writes stay closed.
    assert (
        http.get(f"{INGEST}/api/v1/integrations/{other_iid}/agent-config", headers=auth).status_code
        == 401
    )
    assert http.get(f"{INGEST}/api/v1/integrations/{iid}/setup", headers=auth).status_code == 404
    assert http.get(f"{INGEST}/agent/README.md").status_code == 404
    assert http.post(f"{INGEST}/agent/install.sh").status_code == 404
    assert http.get(f"{INGEST}/api/v1/integrations/{iid}", headers=auth).status_code == 404


def test_ingest_token_cannot_read_anything(http: httpx.Client, admin: Admin) -> None:
    _, token = admin.create()
    auth = {"Authorization": f"Bearer {token}"}
    assert http.get(f"{API}/api/v1/integrations", headers=auth).status_code == 401
    assert http.get(f"{API}/internal/ingest-auth", headers=auth).status_code == 401


# ------------------------------------------------------------------ identity


def test_spoofed_identity_is_overwritten_on_every_signal(http: httpx.Client, admin: Admin) -> None:
    iid, token = admin.create()
    victim = str(uuid.uuid4())
    run = uuid.uuid4().hex[:10]
    resource = {
        "service.name": f"spoof-{run}",
        "sentinel.integration.id": victim,
        "sentinel.integration.kind": "aws_account",
    }
    trace_id = secrets.token_hex(16)
    spoof_headers = {
        "Authorization": f"Bearer {token}",
        "X-Sentinel-Integration-Id": victim,
        "X-Sentinel-Integration-Kind": "aws_account",
    }
    for path, body in (
        ("/v1/metrics", metrics_body(resource, "e2e.spoof", {"run": run})),
        ("/v1/logs", logs_body(resource, f"spoof line {run}")),
        ("/v1/traces", traces_body(resource, trace_id, f"spoof-span-{run}")),
    ):
        assert http.post(f"{INGEST}{path}", json=body, headers=spoof_headers).status_code == 200

    series = eventually(lambda: prom_series(http, f'e2e_spoof_ratio{{run="{run}"}}'))
    assert series[0]["metric"]["sentinel_integration_id"] == iid
    assert series[0]["metric"]["sentinel_integration_kind"] == "otel_app"

    streams = eventually(lambda: loki_streams(http, f'{{service_name="spoof-{run}"}}'))
    assert {s["stream"]["sentinel_integration_id"] for s in streams} == {iid}

    def trace_attrs() -> dict[str, Any] | None:
        r = http.get(f"{JAEGER}/api/v3/traces/{trace_id}")
        if r.status_code != 200:
            return None
        attrs = r.json()["result"]["resourceSpans"][0]["resource"]["attributes"]
        return {a["key"]: a["value"].get("stringValue") for a in attrs}

    attrs = eventually(trace_attrs)
    assert attrs["sentinel.integration.id"] == iid
    assert attrs["sentinel.integration.kind"] == "otel_app"


# ------------------------------------------------------------- normalisation


def test_attribute_variants_and_severity_are_normalised(http: httpx.Client, admin: Admin) -> None:
    _, token = admin.create()
    run = uuid.uuid4().hex[:10]
    resource = {
        "service.name": f"norm-{run}",
        "host": f"h-{run}",
        "deployment.environment": "stage",
    }
    assert (
        send(http, "/v1/logs", logs_body(resource, "boom", severity="err"), token).status_code
        == 200
    )

    streams = eventually(lambda: loki_streams(http, f'{{service_name="norm-{run}"}}'))
    labels = streams[0]["stream"]
    assert labels["host_name"] == f"h-{run}"
    assert labels["deployment_environment_name"] == "stage"
    assert labels["severity_number"] == "17"  # "err" -> ERROR


def test_missing_service_name_comes_from_the_container(http: httpx.Client, admin: Admin) -> None:
    """Checked in Prometheus: Loki would invent a service_name on its own."""
    _, token = admin.create()
    run = uuid.uuid4().hex[:10]
    body = metrics_body({"container.name": f"worker-{run}"}, "e2e.container", {"run": run})
    assert send(http, "/v1/metrics", body, token).status_code == 200
    series = eventually(lambda: prom_series(http, f'e2e_container_ratio{{run="{run}"}}'))
    assert series[0]["metric"]["service_name"] == f"worker-{run}"


def test_high_cardinality_metric_labels_are_dropped(http: httpx.Client, admin: Admin) -> None:
    _, token = admin.create()
    run = uuid.uuid4().hex[:10]
    point = {"run": run, "http.route": "/orders", "user.id": "u-123", "url.full": "http://x/a?b=1"}
    body = metrics_body({"service.name": f"card-{run}"}, "e2e.card", point)
    assert send(http, "/v1/metrics", body, token).status_code == 200
    labels = eventually(lambda: prom_series(http, f'e2e_card_ratio{{run="{run}"}}'))[0]["metric"]
    assert labels["http_route"] == "/orders"
    assert "user_id" not in labels
    assert "url_full" not in labels


# ----------------------------------------------------------------- redaction


def test_secrets_are_redacted_before_storage(http: httpx.Client, admin: Admin) -> None:
    _, token = admin.create()
    run = uuid.uuid4().hex[:10]
    secret_words = "hunter2hunter2"
    bearer = "abc123.def456.ghi789"
    aws = "AKIAIOSFODNN7EXAMPLE"
    line = f"login failed password={secret_words} auth=Bearer {bearer} key {aws} run {run}"
    attrs = {"api_key": "attr-secret-value", "note": f"token: Bearer {bearer}", "safe": "visible"}
    body = logs_body({"service.name": f"red-{run}"}, line, attrs=attrs)
    assert send(http, "/v1/logs", body, token).status_code == 200

    streams = eventually(lambda: loki_streams(http, f'{{service_name="red-{run}"}}'))
    dumped = str(streams)
    for leaked in (secret_words, bearer, aws, "attr-secret-value"):
        assert leaked not in dumped, f"{leaked} reached Loki"
    assert "[REDACTED]" in dumped
    assert streams[0]["stream"]["safe"] == "visible"


# ------------------------------------------------------------ token lifecycle


def test_rate_limit_is_enforced_at_the_door(http: httpx.Client, admin: Admin) -> None:
    _, token = admin.create(rate_limit_per_min=3)
    body = logs_body({"service.name": "limited"}, "x")
    codes = [send(http, "/v1/logs", body, token) for _ in range(5)]
    assert 429 in [r.status_code for r in codes]
    limited = next(r for r in codes if r.status_code == 429)
    assert 1 <= int(limited.headers["Retry-After"]) <= 60


def test_revoked_and_rotated_tokens_stop_working(http: httpx.Client, admin: Admin) -> None:
    iid, token = admin.create()
    body = logs_body({"service.name": "lifecycle"}, "x")
    assert send(http, "/v1/logs", body, token).status_code == 200

    r = admin.client.post(f"/api/v1/integrations/{iid}/tokens/rotate", headers=admin.h)
    new = r.json()["ingest_token"]
    assert send(http, "/v1/logs", body, token).status_code == 401
    assert send(http, "/v1/logs", body, new).status_code == 200

    admin.client.post(f"/api/v1/integrations/{iid}/tokens/revoke", headers=admin.h)
    assert send(http, "/v1/logs", body, new).status_code == 401


# -------------------------------------------------------------------- health


def test_pending_then_healthy_then_stale(http: httpx.Client, admin: Admin) -> None:
    iid, token = admin.create(expected_signals=["logs"])
    assert admin.status(iid)["status"] == "pending"
    body = logs_body({"service.name": "health"}, "alive")
    assert send(http, "/v1/logs", body, token).status_code == 200
    assert eventually(lambda: admin.status(iid)["status"] == "healthy", timeout=20, every=0.5)
    assert admin.status(iid)["signals"][0]["signal"] == "logs"
    # stack-up-fast: stale after 8 s of silence
    assert eventually(lambda: admin.status(iid)["status"] == "stale", timeout=30, every=1)


def test_missing_expected_signal_is_degraded(http: httpx.Client, admin: Admin) -> None:
    iid, token = admin.create(expected_signals=["logs", "traces"])
    assert send(http, "/v1/logs", logs_body({"service.name": "d"}, "x"), token).status_code == 200

    def degraded() -> bool:
        current = admin.status(iid)
        return current["status"] == "degraded" and "traces" in current["status_reason"]

    assert eventually(degraded, timeout=20, every=0.5)


# ------------------------------------------------------------------ entities


def test_entities_and_topology_are_discovered(http: httpx.Client, admin: Admin) -> None:
    iid, token = admin.create()
    run = uuid.uuid4().hex[:10]
    resource = {
        "service.name": f"shop-{run}",
        "service.namespace": "e2e",
        "host.name": f"node-{run}",
        "deployment.environment.name": "test",
    }
    # Logs only: discovery must not depend on the source sending metrics.
    assert send(http, "/v1/logs", logs_body(resource, "hello"), token).status_code == 200

    def service() -> dict[str, Any] | None:
        found = admin.client.get(
            "/api/v1/entities", params={"type": "service", "q": f"shop-{run}"}
        ).json()
        return found[0] if found else None

    entity = eventually(service, timeout=60, every=2)
    assert entity["key"] == f"e2e/shop-{run}@test"
    assert iid in entity["integration_ids"]

    relations = admin.client.get(f"/api/v1/entities/{entity['id']}/relations").json()
    assert [r["relation"] for r in relations] == ["runs_on"]
    host = admin.client.get(f"/api/v1/entities/{relations[0]['to_entity_id']}").json()
    assert host["type"] == "host"
    assert host["display_name"] == f"node-{run}"
