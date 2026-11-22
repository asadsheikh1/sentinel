"""External health checks end to end (Step 9 acceptance).

A small HTTP target (the pinned Caddy image answering "ok") runs on the demo
network as ``sentinel-probe-target``, allowlisted in the dev stack like the demo
shop. The prober checks it, the target is stopped and started, and the results
must show up in Prometheus, in the check's state, and as events.

Needs `make stack-up-fast` (5 s minimum interval, 2 s state passes).
"""

import re
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest

from .test_ingest_e2e import PROM, Admin, eventually

pytestmark = pytest.mark.gateway

TARGET = "sentinel-probe-target"
INTERVAL = 10
OBS = Path(__file__).resolve().parents[3] / "deploy" / "compose" / "compose.observability.yaml"


def _caddy_image() -> str:
    match = re.search(r"docker\.io/library/caddy:\S+", OBS.read_text())
    assert match
    return match.group(0)


def _podman(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["podman", *args], capture_output=True, text=True, check=check, timeout=120
    )


@pytest.fixture(scope="module")
def target() -> Iterator[str]:
    _podman("rm", "-f", TARGET, check=False)
    _podman(
        "run", "-d", "--name", TARGET, "--network", "sentinel-ingest", _caddy_image(),
        "caddy", "respond", "--listen", ":8000", "--body", "ok",
    )  # fmt: skip
    yield TARGET
    _podman("rm", "-f", TARGET, check=False)


def _create_check(admin: Admin, iid: str, **body: Any) -> httpx.Response:
    payload = {
        "integration_id": iid,
        "name": f"e2e-{uuid.uuid4().hex[:6]}",
        "kind": "http",
        "target": f"http://{TARGET}:8000/",
        "interval_s": INTERVAL,
        "timeout_s": 3,
        "assertions": {"body_contains": "ok"},
        **body,
    }
    return admin.client.post("/api/v1/checks", json=payload, headers=admin.h)


def _samples(http: httpx.Client, check_id: str) -> list[tuple[float, float]]:
    r = http.get(
        f"{PROM}/api/v1/query",
        params={"query": f'probe_success{{sentinel_check_id="{check_id}"}}[5m]'},
    )
    r.raise_for_status()
    result = r.json()["data"]["result"]
    return [(float(t), float(v)) for t, v in result[0]["values"]] if result else []


def _state(admin: Admin, check_id: str) -> str:
    return str(admin.get(f"/api/v1/checks/{check_id}")["state"])


def test_endpoint_down_is_visible_within_two_intervals(
    http: httpx.Client, admin: Admin, target: str
) -> None:
    iid, _ = admin.create(kind="http_check", expected_signals=["metrics"])
    r = _create_check(admin, iid)
    assert r.status_code == 201, r.text
    check = r.json()
    cid = check["id"]

    # Up: results arrive and the state follows.
    eventually(lambda: any(v == 1 for _, v in _samples(http, cid)), timeout=60)
    eventually(lambda: _state(admin, cid) == "up", timeout=30)

    # Down: stop the target. A failed result must be in Prometheus within 2 intervals.
    stopped = time.time()
    _podman("stop", "-t", "1", target)
    failed = eventually(
        lambda: [t for t, v in _samples(http, cid) if v == 0 and t >= stopped - 1], timeout=60
    )
    assert failed[0] - stopped <= 2 * INTERVAL, (
        f"first failure {failed[0] - stopped:.1f}s after stop"
    )
    eventually(lambda: _state(admin, cid) == "down", timeout=3 * INTERVAL + 10)
    print(  # published in the step report
        f"first failed result {failed[0] - stopped:.1f}s after stop, "
        f"state down {time.time() - stopped:.1f}s after stop (interval {INTERVAL}s)"
    )

    # Recovery.
    _podman("start", target)
    eventually(lambda: _state(admin, cid) == "up", timeout=3 * INTERVAL + 10)

    # The endpoint entity is shared by every check of this URL (earlier runs too).
    events = [
        e
        for e in admin.get(f"/api/v1/events?type=probe_state&entity={check['entity_id']}")
        if e["payload"]["check_id"] == cid
    ]
    transitions = [(e["payload"]["from"], e["payload"]["to"]) for e in reversed(events)]
    assert transitions == [("unknown", "up"), ("up", "down"), ("down", "up")]
    assert [e["severity"] for e in reversed(events)] == ["info", "critical", "info"]

    # The failure reason is in the logs, not in metric labels.
    labels = http.get(
        f"{PROM}/api/v1/series", params={"match[]": f'probe_success{{sentinel_check_id="{cid}"}}'}
    ).json()
    assert all("error" not in key for series in labels["data"] for key in series)

    # The check's own integration is healthy because its results are arriving.
    eventually(lambda: admin.status(iid)["status"] == "healthy", timeout=30)

    # Leave nothing behind: a leftover check would alert forever on a target that's gone.
    assert admin.client.delete(f"/api/v1/checks/{cid}", headers=admin.h).status_code == 204


def test_unsafe_targets_are_refused_at_creation(admin: Admin) -> None:
    iid, _ = admin.create(kind="http_check")
    for target in [
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost:8000/",
        "http://api:8000/healthz",  # a SENTINEL container name
        "http://postgres:5432/",
        "http://[::1]/",
        "http://2130706433/",
    ]:
        r = _create_check(admin, iid, target=target)
        assert r.status_code == 422, (target, r.text)


def test_prober_cannot_reach_sentinel_internals() -> None:
    """Defence in depth: even without the guard, its network has no route to them."""
    script = (
        "import socket,sys\n"
        "bad=[]\n"
        "for host,port in [('api',8000),('postgres',5432),('prometheus',9090),"
        "('loki',3100),('gateway',4318),('sentinel_api_1',8000),('sentinel_postgres_1',5432)]:\n"
        "    try:\n"
        "        socket.create_connection((host,port),timeout=2).close(); bad.append(host)\n"
        "    except OSError:\n"
        "        pass\n"
        "print(','.join(bad)); sys.exit(1 if bad else 0)\n"
    )
    r = _podman("exec", "sentinel_prober_1", "python", "-c", script, check=False)
    assert r.returncode == 0, f"prober reached: {r.stdout.strip()} {r.stderr.strip()}"
    # It does reach the ingest endpoint (that's how it sends results).
    ok = _podman(
        "exec", "sentinel_prober_1", "python", "-c",
        "import socket; socket.create_connection(('ingest.sentinel.internal',4319),timeout=2)",
        check=False,
    )  # fmt: skip
    assert ok.returncode == 0, ok.stderr
