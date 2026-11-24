"""SENTINEL Agent end to end: install on a fresh machine, see its data, uninstall.

Needs the full stack with short intervals (`make stack-up-fast`). Two targets:

* ``SENTINEL_AGENT_TARGET=container`` (default, `make test-agent`): a disposable
  Ubuntu 24.04 container with systemd as PID 1, on the ``sentinel-ingest``
  network. Container stats come from this machine's Podman socket, mounted in.
* ``SENTINEL_AGENT_TARGET=host`` (CI): installs on the machine running the test,
  a fresh Ubuntu 24.04 VM, with sudo. Container stats and logs come from a
  rootful Podman container started by the test.

Optional: ``SENTINEL_AGENT_PACKAGE_DIR`` holds the otelcol-contrib tarball, its
``.sigstore.json`` and ``cosign-linux-<arch>``, to skip the downloads (the
installer still verifies all of them). ``SENTINEL_AGENT_FOOTPRINT_OUT`` is
where the measured footprint is written as JSON.
"""

import json
import os
import platform
import re
import shlex
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest

pytestmark = pytest.mark.agent

API = os.environ.get("SENTINEL_TEST_API_URL", "http://127.0.0.1:8000")
INGEST = os.environ.get("SENTINEL_TEST_INGEST_URL", "http://127.0.0.1:4319")
PROM = "http://127.0.0.1:9090"
LOKI = "http://127.0.0.1:3100"
TARGET = os.environ.get("SENTINEL_AGENT_TARGET", "container")
IMAGE = os.environ.get("SENTINEL_AGENT_TEST_IMAGE", "localhost/sentinel-test-ubuntu:24.04")
PACKAGE_DIR = os.environ.get("SENTINEL_AGENT_PACKAGE_DIR")
FOOTPRINT_OUT = os.environ.get("SENTINEL_AGENT_FOOTPRINT_OUT")
MACHINE = "sentinel-agent-test"
APP = "agent-e2e-app"  # a container whose stats and logs the agent must pick up
PASSWORD = "agent-e2e-password-1"
ARCH = "arm64" if platform.machine() in {"arm64", "aarch64"} else "amd64"
VERSION = "0.165.0"
WATCHED = ["systemd-journald", "haproxy"]


def eventually(fetch: Callable[[], Any], timeout: float = 120, every: float = 3) -> Any:
    end = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < end:
        try:
            last = fetch()
            if last:
                return last
        except (httpx.HTTPError, KeyError, IndexError, ValueError):
            pass
        time.sleep(every)
    pytest.fail(f"condition not met after {timeout}s; last={last!r}")


# ------------------------------------------------------------------ machine


class Machine:
    """Runs shell commands as root on the machine under test."""

    def __init__(self) -> None:
        self.prefix = ["podman", "exec", "-i", MACHINE] if TARGET == "container" else ["sudo", "-n"]

    def sh(self, script: str, *, input: str | None = None, check: bool = True) -> str:
        r = subprocess.run(
            [*self.prefix, "bash", "-c", script],
            input=input,
            capture_output=True,
            text=True,
            timeout=900,
        )
        if check and r.returncode != 0:
            raise AssertionError(f"`{script}` failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
        return r.stdout

    def ok(self, script: str) -> bool:
        return (
            subprocess.run([*self.prefix, "bash", "-c", script], capture_output=True).returncode
            == 0
        )


def _podman_socket() -> str:
    out = subprocess.run(
        ["podman", "info", "--format", "{{.Host.RemoteSocket.Path}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return out.removeprefix("unix://")


@dataclass
class Installed:
    machine: Machine
    iid: str
    token: str = field(repr=False)  # never printed in test failures or CI logs
    hostname: str
    nonce: str
    config_hash: str
    install_s: float
    healthy_s: float
    output: str = field(repr=False)


class Admin:
    def __init__(self, client: httpx.Client, csrf: str) -> None:
        self.client = client
        self.h = {"X-CSRF-Token": csrf}

    def create(self, **body: Any) -> dict[str, Any]:
        r = self.client.post("/api/v1/integrations", json=body, headers=self.h)
        assert r.status_code == 201, r.text
        return r.json()  # type: ignore[no-any-return]

    def get(self, path: str) -> Any:
        r = self.client.get(path)
        r.raise_for_status()
        return r.json()


@pytest.fixture(scope="module")
def admin() -> Iterator[Admin]:
    email = f"agent-e2e-{uuid.uuid4().hex[:8]}@example.com"
    subprocess.run(
        [sys.executable, "-m", "sentinel.cli", "create-admin", "--email", email, "--name", "Agent",
         "--password-stdin"],
        input=PASSWORD + "\n", text=True, check=True, capture_output=True,
    )  # fmt: skip
    with httpx.Client(base_url=API, timeout=20) as client:
        r = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        r.raise_for_status()
        yield Admin(client, r.json()["csrf_token"])


@pytest.fixture(scope="module")
def http() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=20) as client:
        yield client


def _start_machine() -> tuple[Machine, str, list[str]]:
    """Returns the machine, the endpoint it uses, and extra installer flags."""
    if TARGET == "container":
        subprocess.run(["podman", "rm", "-f", MACHINE], capture_output=True)
        mounts = ["-v", f"{_podman_socket()}:/engine/podman.sock"]
        if PACKAGE_DIR:
            mounts += ["-v", f"{PACKAGE_DIR}:/pkg:ro"]
        subprocess.run(
            ["podman", "run", "-d", "--name", MACHINE, "--hostname", MACHINE,
             "--network", "sentinel-ingest", "--privileged", *mounts, IMAGE],
            check=True, capture_output=True,
        )  # fmt: skip
        machine = Machine()
        eventually(lambda: machine.ok("systemctl is-system-running --wait >/dev/null 2>&1 || "
                                      "systemctl is-system-running | grep -qE 'running|degraded'"),
                   timeout=60, every=1)  # fmt: skip
        # A minimal image: only curl, to fetch the installer the way a user does.
        machine.sh(
            "apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null 2>&1"
            " || dnf install -y -q curl-minimal ca-certificates >/dev/null 2>&1 || true"
        )
        return (
            machine,
            "http://ingest.sentinel.internal:4319",
            ["--engine-socket", "/engine/podman.sock"],
        )
    machine = Machine()
    # A rootful Podman container with journald logs: what the agent must find.
    machine.sh("systemctl enable --now podman.socket >/dev/null 2>&1")
    machine.sh(
        f"podman rm -f {APP} >/dev/null 2>&1; podman run -d --name {APP} --log-driver journald "
        "docker.io/library/busybox:1.37 sh -c "
        "'while true; do echo agent-e2e-app-line $(date +%s); sleep 2; done' >/dev/null"
    )
    return machine, "http://127.0.0.1:4319", []


@pytest.fixture(scope="module")
def installed(admin: Admin) -> Iterator[Installed]:
    machine, endpoint, flags = _start_machine()
    created = admin.create(
        name=f"agent-e2e-{uuid.uuid4().hex[:8]}",
        kind="container_host",
        environment="dev",
        config={
            "watched_processes": WATCHED,
            "collection_interval_s": 10,
            # On a VM running the dev stack, Jaeger's dev ports hold 4317/4318
            # (the installer refuses to start on a clash, which is tested too).
            **({"otlp_grpc_port": 14317, "otlp_http_port": 14318} if TARGET == "host" else {}),
        },
        expected_signals=["metrics", "logs"],
    )
    iid, token = created["integration"]["id"], created["ingest_token"]
    setup = admin.get(f"/api/v1/integrations/{iid}/setup")
    # Run what the setup page says, pointed at this machine's endpoint.
    command = setup["install_command"].replace(setup["endpoint"], endpoint)
    assert "sentinel-agent-install.sh" in command
    assert token not in command
    machine.sh("umask 077; cat > /root/sentinel-token", input=token)
    if PACKAGE_DIR:
        pkg = "/pkg" if TARGET == "container" else PACKAGE_DIR
        flags += ["--package-file", f"{pkg}/otelcol-contrib_{VERSION}_linux_{ARCH}.tar.gz",
                  "--cosign-file", f"{pkg}/cosign-linux-{ARCH}"]  # fmt: skip
    started = time.monotonic()
    output = machine.sh(
        f"cd /root && {command.replace('sudo ', '')} --token-file /root/sentinel-token "
        + " ".join(shlex.quote(f) for f in flags)
    )
    install_s = time.monotonic() - started
    if FOOTPRINT_OUT:  # the installer's own step timings, for the report
        Path(FOOTPRINT_OUT).with_suffix(".install.log").write_text(output)

    def healthy() -> bool:
        return bool(admin.get(f"/api/v1/integrations/{iid}/status")["status"] == "healthy")

    eventually(healthy, timeout=120, every=2)
    state = dict(line.split("=", 1) for line in machine.sh("cat /opt/sentinel-agent/state").split())
    yield Installed(
        machine=machine,
        iid=iid,
        token=token,
        hostname=machine.sh("cat /proc/sys/kernel/hostname").strip(),
        nonce=state["verify_nonce"],
        config_hash=state["config_hash"],
        install_s=install_s,
        healthy_s=time.monotonic() - started,
        output=output,
    )
    if TARGET == "container" and not os.environ.get("SENTINEL_AGENT_KEEP_MACHINE"):
        subprocess.run(["podman", "rm", "-f", MACHINE], capture_output=True)
    if TARGET == "host":
        machine.sh(f"podman rm -f {APP} >/dev/null 2>&1", check=False)


def prom(http: httpx.Client, query: str) -> list[dict[str, Any]]:
    r = http.get(f"{PROM}/api/v1/query", params={"query": query})
    r.raise_for_status()
    return r.json()["data"]["result"]  # type: ignore[no-any-return]


def loki(http: httpx.Client, query: str) -> list[str]:
    now = time.time_ns()
    r = http.get(
        f"{LOKI}/loki/api/v1/query_range",
        params={"query": query, "start": now - 15 * 60 * 10**9, "end": now, "limit": 100},
    )
    r.raise_for_status()
    return [line for s in r.json()["data"]["result"] for _, line in s["values"]]


# ------------------------------------------------------------------ install


def test_install_verifies_the_download_and_reaches_healthy_within_2_minutes(
    installed: Installed,
) -> None:
    assert "Checksum OK" in installed.output
    assert "Signature OK (signed by the OpenTelemetry release workflow" in installed.output
    assert installed.token not in installed.output
    # The acceptance criterion: from running the installer to `healthy`, downloads
    # included. Once the agent runs, data arrives within seconds.
    assert installed.healthy_s - installed.install_s < 60
    assert installed.healthy_s < 120, (
        f"healthy after {installed.healthy_s:.0f}s (installer took {installed.install_s:.0f}s)"
    )


def test_agent_runs_unprivileged_and_listens_only_on_loopback(installed: Installed) -> None:
    m = installed.machine
    pid = _agent_pid(m)
    assert m.sh(f"ps -o user= -p {pid}").split() == ["sentinel-agent"]
    assert m.sh("id -nG sentinel-agent").split() == [
        "sentinel-agent"
    ]  # journal access via the unit
    status = m.sh(f"grep -E '^(CapEff|NoNewPrivs)' /proc/{pid}/status")
    assert re.search(r"CapEff:\s+0+\n", status), status
    assert re.search(r"NoNewPrivs:\s+1", status), status
    listeners = m.sh(f"ss -Hltnup | grep 'pid={pid},' || true").strip().splitlines()
    assert listeners, "expected the loopback OTLP and self-metrics listeners"
    for line in listeners:
        local = line.split()[4]
        assert local.startswith(("127.0.0.1:", "[::1]:")), line


def test_secrets_live_only_in_the_0600_env_file(installed: Installed) -> None:
    m = installed.machine
    assert m.sh("stat -c '%a %U %G' /etc/sentinel-agent/agent.env").strip() == "600 root root"
    assert m.sh("stat -c '%a %U %G' /etc/sentinel-agent/config.yaml").strip() == (
        "640 root sentinel-agent"
    )
    assert installed.token in m.sh("cat /etc/sentinel-agent/agent.env")
    assert not m.ok(
        f"grep -rq {installed.token} /etc/sentinel-agent/config.yaml /etc/systemd/system"
    )
    assert not m.ok("runuser -u sentinel-agent -- cat /etc/sentinel-agent/agent.env")


# ------------------------------------------------------------------ data


def test_host_metrics_are_attributed_to_this_host(installed: Installed, http: httpx.Client) -> None:
    sel = f'sentinel_integration_id="{installed.iid}",host_name="{installed.hostname}"'
    metrics = [
        "system_cpu_time_seconds_total",
        "system_memory_usage_bytes",
        "system_network_io_bytes_total",
        "system_disk_io_bytes_total",
    ]
    # The filesystem scraper reports physical filesystems only, and skips a disk
    # whose device node is missing: true of every disk inside a rootless test
    # container (its root is overlay). On a VM (CI) the root disk is reported.
    if TARGET == "host":
        metrics.append("system_filesystem_usage_bytes")
    for metric in metrics:
        assert eventually(lambda m=metric: prom(http, f"{m}{{{sel}}}"), timeout=60), metric


def test_watched_processes_and_only_those(installed: Installed, http: httpx.Client) -> None:
    sel = f'sentinel_integration_id="{installed.iid}"'
    series = eventually(lambda: prom(http, f"process_cpu_time_seconds_total{{{sel}}}"), timeout=60)
    names = {s["metric"].get("process_executable_name") for s in series}
    assert names <= set(WATCHED), names
    assert "systemd-journald" in names


def test_config_hash_and_agent_self_telemetry_arrive(
    installed: Installed, http: httpx.Client
) -> None:
    sel = (
        f'sentinel_integration_id="{installed.iid}",'
        f'sentinel_agent_config_hash="{installed.config_hash}"'
    )
    assert eventually(lambda: prom(http, f"system_memory_usage_bytes{{{sel}}}"), timeout=60)
    assert eventually(
        lambda: prom(http, f"otelcol_exporter_sent_metric_points_total{{{sel}}}"), timeout=60
    )


def test_verification_marker_arrives_through_the_journal(
    installed: Installed, http: httpx.Client
) -> None:
    query = f'{{sentinel_integration_id="{installed.iid}"}} |= "sentinel-verify-{installed.nonce}"'
    lines = eventually(lambda: loki(http, query), timeout=60)
    assert any(f"integration={installed.iid}" in line for line in lines)


def test_container_metrics_are_attributed_to_containers(
    installed: Installed, http: httpx.Client
) -> None:
    sel = f'sentinel_integration_id="{installed.iid}",container_name!=""'
    series = eventually(lambda: prom(http, f"container_cpu_usage_total_nanoseconds_total{{{sel}}}"
                                     f" or container_memory_usage_total_bytes{{{sel}}}"),
                        timeout=90)  # fmt: skip
    names = {s["metric"]["container_name"] for s in series}
    expected = APP if TARGET == "host" else "sentinel_caddy_1"
    assert expected in names, names
    for s in series:
        assert s["metric"]["host_name"] == installed.hostname


def test_container_logs_are_attributed_to_the_container(
    installed: Installed, http: httpx.Client
) -> None:
    if TARGET != "host":
        pytest.skip("the test container can't see the outer machine's container journal")
    query = (
        f'{{sentinel_integration_id="{installed.iid}",service_name="{APP}"}}'
        ' |= "agent-e2e-app-line"'
    )
    assert eventually(lambda: loki(http, query), timeout=90)


def test_host_and_container_entities_are_discovered(installed: Installed, admin: Admin) -> None:
    def host() -> list[dict[str, Any]]:
        found = admin.get(f"/api/v1/entities?type=host&q={installed.hostname}")
        return [e for e in found if installed.iid in e["integration_ids"]]

    assert eventually(host, timeout=90)
    containers = eventually(
        lambda: [e for e in admin.get("/api/v1/entities?type=container&limit=500")
                 if installed.iid in e["integration_ids"]],
        timeout=90,
    )  # fmt: skip
    assert containers


# ------------------------------------------------------------------ socket proxy


def _agent_pid(m: Machine) -> str:
    """The agent's PID from systemd: a VM may run other Collectors (SENTINEL's gateway)."""
    pid = m.sh("systemctl show -p MainPID --value sentinel-agent").strip()
    assert pid not in {"", "0"}, "sentinel-agent is not running"
    return pid


def _proxy(m: Machine, method: str, path: str) -> str:
    return m.sh(
        "runuser -u sentinel-agent -- curl -s -o /dev/null -w '%{http_code}' "
        f"--unix-socket /run/sentinel-agent-proxy/engine.sock -X {method} 'http://engine{path}'"
    ).strip()


def test_socket_proxy_allows_only_the_reads_the_agent_needs(installed: Installed) -> None:
    m = installed.machine
    assert _proxy(m, "GET", "/v1.41/containers/json") == "200"
    assert _proxy(m, "GET", "/_ping") == "200"
    denied = [
        # Writes to allowed paths: only the method rule stops these.
        ("POST", "/v1.41/containers/json"),
        ("DELETE", "/_ping"),
        ("PUT", "/containers/sentinel_api_1/json"),
        # Paths outside the allowlist.
        ("POST", "/v1.41/containers/create"),
        ("POST", "/containers/sentinel_api_1/stop"),
        ("DELETE", "/containers/sentinel_api_1"),
        ("PUT", "/containers/x/archive"),
        ("GET", "/containers/sentinel_api_1/logs?stdout=1"),
        ("GET", "/containers/sentinel_api_1/stats?stream=true"),
        ("GET", "/info"),
        ("GET", "/images/json"),
        ("GET", "/secrets"),
        ("GET", "/containers/../info"),
    ]
    for method, path in denied:
        assert _proxy(m, method, path) == "403", (method, path)


def test_agent_user_cannot_reach_the_raw_engine_socket(installed: Installed) -> None:
    socket = installed.machine.sh("sed -n 's/^engine_socket=//p' /opt/sentinel-agent/state").strip()
    assert socket
    code = installed.machine.sh(
        f"runuser -u sentinel-agent -- curl -s -o /dev/null -w '%{{http_code}}' "
        f"--unix-socket {socket} http://engine/_ping || true"
    ).strip()
    assert code == "000"  # connection refused by file permissions


# ------------------------------------------------------------------ footprint


def test_footprint(installed: Installed) -> None:
    """RSS and CPU of the agent over 60 s (published in docs/progress/step-08.md)."""
    m = installed.machine
    script = r"""
pid=$(systemctl show -p MainPID --value sentinel-agent); hz=$(getconf CLK_TCK)
t0=$(awk '{print $14+$15}' /proc/$pid/stat); s0=$(date +%s.%N); peak=0
for i in $(seq 1 12); do
  r=$(awk '/VmRSS/{print $2}' /proc/$pid/status); [ "$r" -gt "$peak" ] && peak=$r; sleep 5
done
t1=$(awk '{print $14+$15}' /proc/$pid/stat); s1=$(date +%s.%N)
rss=$(awk '/VmRSS/{print $2}' /proc/$pid/status)
anon=$(awk '/RssAnon/{print $2}' /proc/$pid/status)
file=$(awk '/RssFile/{print $2}' /proc/$pid/status)
hp=$(systemctl show -p MainPID --value sentinel-agent-socket-proxy || true)
[ "$hp" = 0 ] && hp=""
proxy=$([ -n "$hp" ] && awk '/VmRSS/{print $2}' /proc/$hp/status || echo 0)
cpu=$(awk -v a="$t0" -v b="$t1" -v hz="$hz" -v s0="$s0" -v s1="$s1" \
  'BEGIN{printf "%.2f", (b-a)/hz/(s1-s0)*100}')
echo "{\"rss_kib\": $rss, \"rss_anon_kib\": $anon, \"rss_file_kib\": $file,"\
" \"peak_rss_kib\": $peak, \"proxy_rss_kib\": $proxy,"\
" \"cpu_percent\": $cpu, \"cpus\": $(nproc)}"
"""
    result = json.loads(m.sh(script))
    result.update(
        target=TARGET, install_s=round(installed.install_s), healthy_s=round(installed.healthy_s)
    )
    print("footprint:", json.dumps(result))
    if FOOTPRINT_OUT:
        Path(FOOTPRINT_OUT).write_text(json.dumps(result, indent=2))
    assert result["peak_rss_kib"] < 400 * 1024  # the unit's MemoryMax
    assert result["cpu_percent"] < 50  # the unit's CPUQuota


def test_container_event_stream_survives_idle_periods(installed: Installed) -> None:
    """Runs after the 60 s footprint test: /events must not be cut while idle."""
    errors = installed.machine.sh(
        "journalctl -u sentinel-agent --no-pager -o cat | "
        "grep -c 'Error watching docker container events' || true"
    ).strip()
    assert errors == "0", f"{errors} event-stream errors in the agent journal"


# ------------------------------------------------------------------ detection (Step 10)


def _signals(admin: Admin, **params: str) -> list[dict[str, Any]]:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return list(admin.get(f"/api/v1/signals?{query}&limit=500"))


ENOSPC_TRACE = """Traceback (most recent call last):
  File "/srv/shop/src/shop/receipts.py", line 41, in write_receipt
    fh.write(pdf)
OSError: [Errno 28] No space left on device
"""


def _app_logs(service: str, host: str, n: int, *, error: bool) -> dict[str, Any]:
    """OTLP logs from an app running on ``host`` (as an instrumented app would send them)."""
    now = time.time_ns()
    attrs = (
        {"exception.type": "OSError", "exception.message": "[Errno 28] No space left on device",
         "exception.stacktrace": ENOSPC_TRACE, "code.file.path": "/srv/shop/src/shop/receipts.py",
         "code.function.name": "write_receipt"}
        if error else {}
    )  # fmt: skip
    records = [
        {
            "timeUnixNano": str(now + i),
            "severityNumber": 17 if error else 9,
            "severityText": "ERROR" if error else "INFO",
            "body": {"stringValue": "receipt write failed" if error else "started"},
            "attributes": [{"key": k, "value": {"stringValue": v}} for k, v in attrs.items()],
        }
        for i in range(n)
    ]
    resource = {"service.name": service, "service.namespace": "e2e", "host.name": host}
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {"key": k, "value": {"stringValue": v}} for k, v in resource.items()
                    ]
                },
                "scopeLogs": [{"scope": {"name": "shop.receipts"}, "logRecords": records}],
            }
        ]
    }


def _dump_incidents(admin: Admin, host_entity: str, svc: dict[str, Any]) -> None:
    """On failure: what the correlator saw (printed to the CI log)."""
    print("host entity:", admin.get(f"/api/v1/entities/{host_entity}"))
    print("service entity:", svc)
    print("service relations:", admin.get(f"/api/v1/entities/{svc['id']}/relations"))
    for inc in admin.get("/api/v1/incidents?limit=20"):
        d = admin.get(f"/api/v1/incidents/{inc['id']}")
        print(inc["ref"], inc["status"], inc["title"])
        print("  entities:", [(e["type"], e["key"], e["role"]) for e in d["entities"]])
        for s in d["signals"]:
            print("  signal:", s["kind"], s["reason"], s["entity_id"], s["starts_at"], s["labels"])


def test_disk_filling_up_raises_a_host_signal(installed: Installed, admin: Admin) -> None:
    """S13: a fill file pushes the root filesystem past 90% (a host signal), and an app
    on that host fails to write (its errors): one incident with both entities (Step 12).

    The app's errors are sent by the test, as the app would send them: the disk is
    filled to 93%, not 100%, so nothing on the runner really fails to write."""
    if TARGET != "host":
        pytest.skip("needs a real disk (a VM); a test container's filesystems are not physical")
    m = installed.machine
    fill = "/root/sentinel-e2e-fill"  # on the root filesystem, root-only
    # Reserve (instantly, no writes) enough to reach 93% used.
    m.sh(
        'python3 -c \'import os; s=os.statvfs("/"); '
        "need=int(s.f_bavail*s.f_frsize - 0.07*s.f_blocks*s.f_frsize); print(max(need,0))' "
        f"| xargs -I{{}} fallocate -l {{}} {fill}"
    )
    try:

        def host_signal() -> list[dict[str, Any]]:
            return [
                s
                for s in _signals(admin, kind="prom_alert", status="firing")
                if s["labels"].get("alertname") == "HostFilesystemFull"
                and s["labels"].get("host_name") == installed.hostname
            ]

        found = eventually(host_signal, timeout=360, every=10)
        assert found[0]["severity"] == "critical"
        host_entity = found[0]["entity_id"]
        assert host_entity
        # The host, not the agent's own service (its metrics carry the agent's `job`).
        assert admin.get(f"/api/v1/entities/{host_entity}")["type"] == "host"

        # An app on this host: discovered first (it runs_on the host), then failing.
        service = f"shop-{installed.nonce[:6]}"
        app = admin.create(name=f"e2e-app-{service}", kind="otel_app", expected_signals=["logs"])
        token = {"Authorization": f"Bearer {app['ingest_token']}"}
        with httpx.Client(timeout=10) as http:
            started = _app_logs(service, installed.hostname, 1, error=False)
            assert http.post(f"{INGEST}/v1/logs", json=started, headers=token).status_code == 200
            svc = eventually(
                lambda: admin.get(f"/api/v1/entities?type=service&q={service}"), timeout=60
            )[0]
            failing = _app_logs(service, installed.hostname, 20, error=True)
            assert http.post(f"{INGEST}/v1/logs", json=failing, headers=token).status_code == 200

        def one_incident() -> list[dict[str, Any]]:
            for inc in admin.get("/api/v1/incidents?status=open"):
                detail = admin.get(f"/api/v1/incidents/{inc['id']}")
                ids = {e["id"] for e in detail["entities"]}
                if {host_entity, svc["id"]} <= ids:
                    return [detail]
            return []

        try:
            incident = eventually(one_incident, timeout=120, every=5)[0]
        except BaseException:
            _dump_incidents(admin, host_entity, svc)
            raise
        kinds = {s["kind"] for s in incident["signals"]}
        assert {"prom_alert", "error_group_new"} <= kinds
        assert "related_entity" in {s["reason"] for s in incident["signals"]}
    finally:
        m.sh(f"rm -f {fill}", check=False)


def test_agent_stopped_becomes_a_telemetry_gap(installed: Installed, admin: Admin) -> None:
    """S15-style: the agent stops; a telemetry_gap signal opens, then resolves."""
    m = installed.machine

    def gap(status: str) -> list[dict[str, Any]]:
        return [
            s
            for s in _signals(admin, kind="telemetry_gap", status=status)
            if s["source_integration_id"] == installed.iid
        ]

    m.sh("systemctl stop sentinel-agent")
    try:
        opened = eventually(lambda: gap("firing"), timeout=120, every=3)
        assert opened[0]["payload"]["classification"] in {"host_or_agent", "agent_problem"}
        assert opened[0]["severity"] in {"warning", "info"}
    finally:
        m.sh("systemctl start sentinel-agent")
    eventually(lambda: not gap("firing"), timeout=120, every=3)
    assert gap("resolved")


# ------------------------------------------------------------------ re-run, uninstall


def _endpoint() -> str:
    return (
        "http://ingest.sentinel.internal:4319" if TARGET == "container" else "http://127.0.0.1:4319"
    )


def _cached_flags() -> str:
    flags = "--engine-socket /engine/podman.sock" if TARGET == "container" else ""
    if PACKAGE_DIR:
        pkg = "/pkg" if TARGET == "container" else PACKAGE_DIR
        flags += (f" --package-file {pkg}/otelcol-contrib_{VERSION}_linux_{ARCH}.tar.gz"
                  f" --cosign-file {pkg}/cosign-linux-{ARCH}")  # fmt: skip
    return flags


def test_rerunning_the_installer_updates_in_place(installed: Installed, admin: Admin) -> None:
    m = installed.machine
    before = m.sh("systemctl show -p InvocationID --value sentinel-agent").strip()
    out = m.sh(
        f"cd /root && bash sentinel-agent-install.sh --endpoint {_endpoint()} "
        f"--integration {installed.iid} --token-file /root/sentinel-token {_cached_flags()}"
    )
    assert "is running" in out
    assert m.sh("systemctl show -p InvocationID --value sentinel-agent").strip() != before
    assert m.sh("getent passwd sentinel-agent | wc -l").strip() == "1"
    # A restart leaves a short gap, which the fast test intervals can see as
    # `degraded` for a moment; it must be healthy again shortly.
    eventually(
        lambda: admin.get(f"/api/v1/integrations/{installed.iid}/status")["status"] == "healthy",
        timeout=60,
        every=2,
    )


def test_uninstall_leaves_no_residue(installed: Installed, http: httpx.Client) -> None:
    m = installed.machine
    m.sh(f"curl -fsSL {_endpoint()}/agent/uninstall.sh -o /root/sentinel-agent-uninstall.sh"
         " && bash /root/sentinel-agent-uninstall.sh")  # fmt: skip
    leftovers = m.sh(
        "ls -d /etc/sentinel-agent /opt/sentinel-agent /var/lib/sentinel-agent "
        "/run/sentinel-agent-proxy /etc/systemd/system/sentinel-agent*.service 2>/dev/null; "
        "getent passwd sentinel-agent; getent group sentinel-agent; "
        "systemctl list-units --all --no-legend 'sentinel-agent*' ; true"
    ).strip()
    assert leftovers == "", leftovers
    # Separate call, and [/] in the pattern, so pgrep can't match its own shell.
    processes = m.sh("pgrep -af '[/]opt/sentinel-agent|[/]etc/sentinel-agent' || true").strip()
    assert processes == "", processes
    if m.ok("command -v dpkg"):
        assert not m.ok("dpkg -s haproxy >/dev/null 2>&1")  # it was installed by us
    else:
        assert not m.ok("rpm -q haproxy")
