"""The AVIRO host: the machine the evaluation breaks (``evals/host/aviro-host.sh``).

Everything here acts on that machine the way an operator (or a fault) would:
deploy a git ref, flip an AVIRO runtime fault, stop the broker, fill the disk,
hog the CPU, stop the agent, or take the whole host away. Commands are fixed
templates; scenario values are validated by :mod:`sentinel.evals.scenario` and
passed as arguments, never through a shell string built from free text.
"""

import contextlib
import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MACHINE = "aviro-host"
AVIRO_DIR = "/srv/aviro"
ADMIN_TOKEN = "aviro-admin-dev-only"  # noqa: S105 - AVIRO's dev default; the host is disposable
GATEWAY = "http://127.0.0.1:8080"
FILL_FILE = "/var/lib/containers/sentinel-eval-fill"
CPU_UNIT = "sentinel-eval-cpuhog"
LOCUST_IMAGE = "docker.io/locustio/locust:2.47.0"  # as in AVIRO's compose.yaml
LOADGEN = "sentinel-eval-shoppers"
COMPOSE = "podman-compose -f compose.yaml -f compose.sentinel.yaml"
EVENT_PREFIX = "deployment event (SENTINEL webhook not configured): "


class HostError(RuntimeError):
    pass


@dataclass(frozen=True)
class Deployment:
    ref: str
    commit_sha: str
    version: str
    events: list[dict[str, Any]]


class AviroHost:
    def __init__(self, script: Path, locustfile: Path, *, machine: str = MACHINE) -> None:
        self.script = script
        self.locustfile = locustfile  # AVIRO's, at the ref the host runs
        self.machine = machine

    # ------------------------------------------------------------ plumbing
    def run(self, *args: str, timeout_s: float = 120, check: bool = True) -> str:
        """Run a command on the machine (argv, no shell)."""
        return self._podman("exec", self.machine, *args, timeout_s=timeout_s, check=check)

    def sh(self, script: str, *, timeout_s: float = 120, check: bool = True) -> str:
        """A fixed shell snippet on the machine. Callers quote every value with shlex."""
        return self.run("sh", "-c", script, timeout_s=timeout_s, check=check)

    def _podman(self, *args: str, timeout_s: float, check: bool) -> str:
        try:
            done = subprocess.run(
                ["podman", *args], capture_output=True, text=True, timeout=timeout_s, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise HostError(f"podman {args[0]} timed out after {timeout_s:g} s") from exc
        if check and done.returncode != 0:
            what = " ".join(args[:4])[:120]
            output = (done.stderr or done.stdout).strip()[-500:]
            raise HostError(f"podman {what} (exit {done.returncode}): {output}")
        return done.stdout

    def up(self, token: str, *, timeout_s: float = 1800) -> None:
        done = subprocess.run(
            [str(self.script), "up", token], capture_output=True, text=True,
            timeout=timeout_s, check=False,
        )  # fmt: skip
        if done.returncode != 0:
            raise HostError(f"aviro-host up failed: {done.stderr[-1500:]}")

    def down(self) -> None:
        self.load(0)
        subprocess.run([str(self.script), "down"], capture_output=True, timeout=300, check=False)

    def running(self) -> bool:
        out = self._podman(
            "inspect", "-f", "{{.State.Status}}", self.machine, timeout_s=30, check=False
        )
        return out.strip() == "running"

    # ---------------------------------------------------------------- AVIRO
    def healthy(self) -> bool:
        out = self.run(
            "curl", "-fsS", "-m", "5", f"{GATEWAY}/api/health", check=False, timeout_s=15
        )
        try:
            return bool(json.loads(out).get("ready"))
        except ValueError:
            return False

    def deploy(self, ref: str) -> Deployment:
        out = self.sh(
            f"cd {AVIRO_DIR} && scripts/deploy.sh {shlex.quote(ref)} 2>/dev/null", timeout_s=900
        )
        events = [
            json.loads(line.split(EVENT_PREFIX, 1)[1])
            for line in out.splitlines()
            if EVENT_PREFIX in line
        ]
        if not events:
            raise HostError(f"deploy {ref}: no deployment events in the output")
        return Deployment(
            ref=ref,
            commit_sha=str(events[0]["commit_sha"]),
            version=str(events[0]["version"]),
            events=events,
        )

    def commit_of(self, ref: str) -> str:
        return self.run(
            "git", "-C", AVIRO_DIR, "rev-parse", "--verify", f"{ref}^{{commit}}"
        ).strip()

    def admin(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        args = [
            "curl",
            "-sS",
            "-m",
            "15",
            "-X",
            method,
            "-H",
            f"X-Aviro-Admin-Token: {ADMIN_TOKEN}",
        ]
        if body is not None:
            args += ["-H", "Content-Type: application/json", "--data", json.dumps(body)]
        out = self.run(*args, f"{GATEWAY}{path}", timeout_s=30)
        return json.loads(out) if out.strip() else None

    def set_fault(self, service: str, kind: str, params: dict[str, Any], duration_s: float) -> None:
        path = "/admin/faults" if service == "gateway" else f"/admin/{service}/faults"
        self.admin("POST", path, {"type": kind, "params": params, "duration_s": duration_s})

    def clear_faults(self) -> None:
        for service in ("gateway", "inventory", "payments", "orders"):
            path = "/admin/faults" if service == "gateway" else f"/admin/{service}/faults"
            with contextlib.suppress(HostError, ValueError):  # a service that is down
                self.admin("DELETE", path)

    def scenario_data(self, name: str, apply: bool) -> None:
        self.admin("POST" if apply else "DELETE", f"/admin/inventory/scenario-data/{name}")

    def container(self, name: str, action: str) -> None:
        self.run("podman", action, f"aviro_{name}_1", timeout_s=120)

    def ensure_containers_running(self) -> None:
        self.sh(f"cd {AVIRO_DIR} && {COMPOSE} up -d postgres valkey >/dev/null 2>&1", timeout_s=300)
        self.sh(
            "for c in $(podman ps -a --filter label=io.podman.compose.project=aviro "
            "--filter status=exited --format '{{.Names}}' | grep -v loadgen); "
            "do podman start $c; done",
            timeout_s=300,
            check=False,
        )

    def load(self, users: int) -> None:
        """(Re)start the synthetic shoppers with this many users; 0 stops them.

        Shoppers come from outside, as real traffic does: AVIRO's own Locust image
        and locustfile, run next to SENTINEL (not on the AVIRO host, whose agent
        would collect Locust's console output as the host's logs), through the
        host's edge port.
        """
        self._podman("rm", "-f", "-t", "2", LOADGEN, timeout_s=60, check=False)
        if users <= 0:
            return
        self._podman(
            "run", "-d", "--name", LOADGEN, "--network", "sentinel-ingest",
            "-v", f"{self.locustfile}:/mnt/locustfile.py:ro,z",
            LOCUST_IMAGE, "-f", "/mnt/locustfile.py", "--headless",
            "--host", f"http://{self.machine}:8081", "-u", str(int(users)), "-r", "5",
            "--run-time", "48h",
            timeout_s=120, check=True,
        )  # fmt: skip

    # ----------------------------------------------------------------- host
    def disk_fill(self, fill: bool) -> None:
        if fill:
            # All the free space, reserved blocks included: writes then fail with ENOSPC.
            self.sh(
                f"rm -f {FILL_FILE}; avail=$(df --output=avail -B1 /var/lib/containers | tail -1); "
                f"fallocate -l $avail {FILL_FILE} 2>/dev/null || true; "
                f"dd if=/dev/zero of={FILL_FILE}.tail bs=1M 2>/dev/null || true",
                timeout_s=300,
            )
        else:
            self.sh(f"rm -f {FILL_FILE} {FILL_FILE}.tail", timeout_s=60)

    def disk_used_ratio(self) -> float:
        out = self.sh("df --output=used,size -B1 /var/lib/containers | tail -1")
        used, size = (int(x) for x in out.split())
        return used / size

    def cpu_hog(self, start: bool, threads: int = 4) -> None:
        if start:
            self.sh(
                f"systemctl stop {CPU_UNIT} >/dev/null 2>&1; "
                f"systemd-run --unit={CPU_UNIT} --quiet sh -c "
                f"'for i in $(seq {int(threads)}); do yes >/dev/null & done; wait'"
            )
        else:
            self.sh(f"systemctl stop {CPU_UNIT} >/dev/null 2>&1 || true")

    def agent(self, action: str) -> None:
        self.run("systemctl", action, "sentinel-agent.service", timeout_s=60)

    def agent_active(self) -> bool:
        return (
            self.run("systemctl", "is-active", "sentinel-agent.service", check=False).strip()
            == "active"
        )

    def pause(self, paused: bool) -> None:
        state = self._podman(
            "inspect", "-f", "{{.State.Status}}", self.machine, timeout_s=30, check=False
        ).strip()
        if paused and state == "running":
            self._podman("pause", self.machine, timeout_s=60, check=True)
        elif not paused and state == "paused":
            self._podman("unpause", self.machine, timeout_s=60, check=True)

    def install_agent(
        self, endpoint: str, integration_id: str, token: str, installer_url: str
    ) -> str:
        """The installer, run the way the setup page says (token in a file, never argv)."""
        subprocess.run(
            ["podman", "exec", "-i", self.machine,
             "sh", "-c", "umask 077; cat > /root/sentinel-token"],
            input=token, text=True, check=True, timeout=30,
        )  # fmt: skip
        flags = ""
        has_pkg = self.run(
            "sh",
            "-c",
            "ls /pkg/otelcol-contrib_*_linux_*.tar.gz 2>/dev/null | head -1",
            check=False,
        ).strip()
        if has_pkg:
            arch = "arm64" if "arm64" in has_pkg else "amd64"
            flags = f" --package-file {shlex.quote(has_pkg)} --cosign-file /pkg/cosign-linux-{arch}"
        return self.sh(
            f"cd /root && curl -fsSL --retry 5 {shlex.quote(installer_url)} -o install.sh && "
            f"bash install.sh --endpoint {shlex.quote(endpoint)} "
            f"--integration {shlex.quote(integration_id)} "
            f"--token-file /root/sentinel-token{flags}",
            timeout_s=900,
        )
