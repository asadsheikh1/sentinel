"""The evaluation orchestrator: set up the environment, run each scenario, record, score.

One **suite run**:

1. **Environment.** Start the AVIRO host, onboard it like a user would (an app
   integration, a container-host integration with the agent installed by the
   real installer, an HTTP check, a deploy key), and time how long each
   integration takes to show its first data.
2. **Runs** (repeat-major: every scenario once, then again). For each:
   prepare a clean baseline → inject the fault → watch SENTINEL's API for the
   observation window → record telemetry fixtures → recover → wait for the
   signals to clear → close this run's incidents (true positives resolved and
   closed, the rest marked false positive) so runs can't leak into each other.
3. **Soak** (optional): normal load, no faults, for the false-positive rate.

Everything is written as it happens (``per_run.jsonl``), so an interrupted
suite can be resumed and partial results are never lost.
"""

import asyncio
import contextlib
import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from sentinel.config import Settings
from sentinel.evals import metrics
from sentinel.evals.client import SentinelClient, short_id
from sentinel.evals.host import AviroHost, HostError
from sentinel.evals.scenario import (
    Agent,
    ClearFaults,
    Container,
    CpuHog,
    Data,
    Deploy,
    DiskFill,
    Fault,
    Host,
    Load,
    Scenario,
    Step,
)
from sentinel.telemetry.base import Limits, Selector, TelemetryError, TimeRange
from sentinel.telemetry.fixtures import FixtureTelemetryAdapter, Mode
from sentinel.telemetry.jaeger import JaegerClient
from sentinel.telemetry.loki import LokiClient
from sentinel.telemetry.prometheus import PrometheusClient, Template

log = structlog.get_logger("sentinel.evals")

ACTIVE = {"open", "investigating", "mitigated"}
HOST_NAME = "aviro-host"
AVIRO_NAMESPACE = "aviro"


@dataclass
class SuiteConfig:
    api_url: str
    email: str
    password: str
    scenarios_dir: Path
    results_dir: Path
    fixtures_dir: Path
    host_script: Path
    ingest_endpoint: str = "http://ingest.sentinel.internal:4319"  # as the host sees it
    check_url: str = f"http://{HOST_NAME}:8081/api/health"
    check_interval_s: int = 30
    settle_s: float = 60
    poll_s: float = 15
    cooldown_max_s: float = 900
    healthy_timeout_s: float = 300
    record_fixtures: bool = True
    locustfile: Path = Path("locustfile.py")


@dataclass
class Environment:
    app_integration: str
    host_integration: str
    check_integration: str
    check_id: str
    deploy_key: str
    time_to_first_data_s: dict[str, float | None] = field(default_factory=dict)
    integration_ids: tuple[str, ...] = ()


def now() -> datetime:
    return datetime.now(UTC)


def wait_for(condition: Callable[[], bool], timeout_s: float, every_s: float = 5) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if condition():
                return True
        except (HostError, OSError, ValueError):
            pass
        time.sleep(every_s)
    return False


class Runner:
    def __init__(self, config: SuiteConfig, settings: Settings) -> None:
        self.cfg = config
        self.settings = settings
        self.api = SentinelClient(config.api_url, config.email, config.password)
        self.host = AviroHost(config.host_script, config.locustfile)
        self.env: Environment | None = None
        self.deployed_ref = "main"
        self._entity_cache: dict[str, set[str]] = {}

    # ========================================================= environment
    def setup(self) -> Environment:
        self.disable_leftovers()
        tag = short_id()
        app_id, app_token = self.api.create_integration(
            name=f"eval-aviro-app-{tag}", kind="otel_app", environment="dev",
            expected_signals=["traces", "logs", "metrics"],
        )  # fmt: skip
        host_id, host_token = self.api.create_integration(
            name=f"eval-aviro-host-{tag}", kind="container_host", environment="dev",
            expected_signals=["metrics", "logs"],
        )  # fmt: skip
        check_integration, _ = self.api.create_integration(
            name=f"eval-aviro-check-{tag}", kind="http_check", environment="dev",
            expected_signals=["metrics"],
        )  # fmt: skip
        deploy_key = self.api.create_api_key(f"eval-deploys-{tag}", ["deployments:write"])

        log.info("eval_host_up")
        app_created = time.monotonic()
        self.host.down()
        self.host.up(app_token)
        log.info("eval_agent_install")
        host_created = time.monotonic()
        self.host.install_agent(
            self.cfg.ingest_endpoint, host_id, host_token,
            f"{self.cfg.ingest_endpoint}/agent/install.sh",
        )  # fmt: skip
        check_created = time.monotonic()
        check_id = self.api.create_check(
            integration_id=check_integration, name=f"eval-aviro-health-{tag}", kind="http",
            target=self.cfg.check_url, interval_s=self.cfg.check_interval_s, timeout_s=10,
            assertions={"expected_status": [200]},
        )  # fmt: skip
        env = Environment(
            app_integration=app_id,
            host_integration=host_id,
            check_integration=check_integration,
            check_id=check_id,
            deploy_key=deploy_key,
            integration_ids=(app_id, host_id, check_integration),
        )
        env.time_to_first_data_s = {
            "app": self._time_to_ready(app_id, app_created),
            "host": self._time_to_ready(host_id, host_created),
            "check": self._time_to_ready(check_integration, check_created),
        }
        self.env = env
        return env

    def disable_leftovers(self) -> list[str]:
        """Earlier suite runs' integrations: left enabled, each would go stale and raise gaps."""
        disabled = []
        for integration in self.api.json("GET", "/api/v1/integrations"):
            if (
                integration["name"].startswith("eval-aviro-")
                and integration["status"] != "disabled"
            ):
                self.api.disable_integration(str(integration["id"]))
                disabled.append(integration["name"])
        return disabled

    def _time_to_ready(
        self, integration_id: str, started: float, timeout_s: float = 600
    ) -> float | None:
        """Seconds until every required checklist item ticked (None: not within timeout)."""

        def ready() -> bool:
            items = self.api.integration_status(integration_id)["checklist"]
            return all(i["done"] for i in items if i["required"])

        if wait_for(ready, timeout_s, every_s=3):
            return round(time.monotonic() - started, 1)
        return None

    def teardown(self) -> None:
        if self.env:
            for iid in self.environment.integration_ids:
                self.api.disable_integration(iid)
        self.host.down()

    # ============================================================ helpers
    @property
    def environment(self) -> Environment:
        if self.env is None:
            raise RuntimeError("the environment is not set up (call setup() first)")
        return self.env

    def environment_entities(self) -> set[str]:
        ids: set[str] = set()
        for iid in self.environment.integration_ids:
            for entity in self.api.json(
                "GET", "/api/v1/entities", params={"integration": iid, "limit": 500}
            ):
                ids.add(str(entity["id"]))
        return ids

    def observe_incidents(
        self, since: datetime, env_entities: set[str]
    ) -> list[metrics.IncidentObs]:
        found = []
        for summary in self.api.incidents_since(since):
            detail = self.api.incident(summary["id"])
            entities = tuple(
                metrics.EntityObs(e["type"], e["key"], e["display_name"])
                for e in detail["entities"]
            )
            primary = next(
                (metrics.EntityObs(e["type"], e["key"], e["display_name"])
                 for e in detail["entities"] if e["id"] == detail.get("primary_entity_id")),
                None,
            )  # fmt: skip
            in_env = any(str(e["id"]) in env_entities for e in detail["entities"]) or any(
                s.get("source_integration_id") in (self.environment.integration_ids)
                for s in detail["signals"]
            )
            found.append(
                metrics.IncidentObs(
                    id=detail["id"],
                    ref=detail["ref"],
                    detected_at=datetime.fromisoformat(detail["detected_at"]),
                    started_at=datetime.fromisoformat(detail["started_at"]),
                    severity=detail["severity"],
                    status=detail["status"],
                    service_name=detail.get("service_name"),
                    entities=entities,
                    primary_entity=primary,
                    in_environment=in_env,
                    env_gap=any(
                        s["kind"] == "telemetry_gap"
                        and s.get("source_integration_id") in self.environment.integration_ids
                        for s in detail["signals"]
                    ),
                )
            )
        return found

    def observe_gaps(self, since: datetime, until: datetime) -> list[metrics.GapObs]:
        """Gaps of this environment active during the window.

        A gap that recurs within 10 minutes is *reopened* (same signal, its first
        start kept), so gaps are selected by being active in the window, not by
        when they first started.
        """
        gaps = []
        for s in self.api.signals_since(since - timedelta(hours=1), kind="telemetry_gap"):
            if s.get("source_integration_id") not in self.environment.integration_ids:
                continue
            ended = datetime.fromisoformat(s["ends_at"]) if s.get("ends_at") else None
            started = datetime.fromisoformat(s["starts_at"])
            if started > until or (ended is not None and ended < since):
                continue
            gaps.append(
                metrics.GapObs(
                    classification=str(s["payload"].get("classification", "")),
                    severity=s["severity"],
                    started_at=datetime.fromisoformat(s["starts_at"]),
                )
            )
        return gaps

    def environment_signals(self, since: datetime, env_entities: set[str]) -> list[dict[str, Any]]:
        return [
            s for s in self.api.signals_since(since)
            if str(s.get("entity_id")) in env_entities
            or s.get("source_integration_id") in self.environment.integration_ids
        ]  # fmt: skip

    # ============================================================== steps
    def do(self, step: Step) -> dict[str, Any]:
        """Run one scenario step on the host. Returns what happened (for the run record)."""
        started = now()
        detail: dict[str, Any] = {"step": step.step, "at": started.isoformat()}
        match step:
            case Deploy(ref=ref):
                deployment = self.host.deploy(ref)
                run_id = started.strftime("%Y%m%dT%H%M%SZ")
                for event in deployment.events:
                    key = f"eval-{deployment.commit_sha[:12]}-{event['service']}-{run_id}"
                    self.api.post_deployment(self.environment.deploy_key, event, key)
                self.deployed_ref = ref
                detail |= {
                    "ref": ref,
                    "commit_sha": deployment.commit_sha,
                    "version": deployment.version,
                }
            case Fault(service=service, type=kind, params=params, duration_s=duration):
                self.host.set_fault(service, kind, dict(params), duration)
                detail |= {"service": service, "type": kind, "params": dict(params)}
            case ClearFaults():
                self.host.clear_faults()
            case Container(name=name, action=action):
                self.host.container(name, action)
                detail |= {"name": name, "action": action}
            case Data(name=name, action=action):
                self.host.scenario_data(name, action == "apply")
                detail |= {"name": name, "action": action}
            case DiskFill(action=action):
                self.host.disk_fill(action == "fill")
                detail |= {"action": action, "disk_used": round(self.host.disk_used_ratio(), 3)}
            case CpuHog(action=action, threads=threads):
                self.host.cpu_hog(action == "start", threads)
                detail |= {"action": action, "threads": threads}
            case Agent(action=action):
                self.host.agent(action)
                detail |= {"action": action}
            case Host(action=action):
                self.host.pause(action == "pause")
                detail |= {"action": action}
            case Load(users=users):
                self.host.load(users)
                detail |= {"users": users}
        return detail

    def run_steps(self, steps: list[Step], t0: float) -> list[dict[str, Any]]:
        done = []
        for step in sorted(steps, key=lambda s: s.after_s):
            wait = t0 + step.after_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            done.append(self.do(step))
        return done

    # ============================================================ baseline
    def baseline(self, load_users: int) -> list[str]:
        """Back to a healthy AVIRO on main with no faults. Returns notes about what it fixed."""
        notes = []
        self.host.pause(False)
        self.host.cpu_hog(False)
        self.host.disk_fill(False)
        if not self.host.agent_active():
            self.host.agent("start")
            notes.append("agent was stopped")
        self.host.ensure_containers_running()
        if self.deployed_ref != "main":
            self.do(Deploy(ref="main"))
        if not wait_for(self.host.healthy, self.cfg.healthy_timeout_s):
            notes.append("AVIRO not healthy after baseline")
        self.host.clear_faults()
        with contextlib.suppress(HostError, ValueError):
            self.host.scenario_data("s10", apply=False)
        self.host.load(load_users)
        return notes

    def clear_before_fault(self, env_entities: set[str]) -> list[str]:
        """No active incident about the environment may be left to absorb this run's signals."""
        notes = []
        if not self.wait_quiet(now() - timedelta(hours=6), env_entities):
            notes.append("signals about the environment still firing at the fault start")
        for incident in self.api.json(
            "GET", "/api/v1/incidents", params={"status": sorted(ACTIVE), "limit": 200}
        ):
            detail = self.api.incident(str(incident["id"]))
            about_env = any(str(e["id"]) in env_entities for e in detail["entities"]) or any(
                s.get("source_integration_id") in self.environment.integration_ids
                for s in detail["signals"]
            )
            if about_env:
                self.api.set_incident_status(str(incident["id"]), "resolved", "evaluation baseline")
                self.api.set_incident_status(str(incident["id"]), "closed", "evaluation baseline")
                notes.append(f"closed leftover {incident['ref']}")
        return notes

    def close_incidents(self, since: datetime, tps: set[str], note: str) -> None:
        for incident in self.api.incidents_since(since):
            if incident["status"] not in ACTIVE:
                continue
            if incident["id"] in tps:
                self.api.set_incident_status(incident["id"], "resolved", note)
                self.api.set_incident_status(incident["id"], "closed", note)
            else:
                self.api.set_incident_status(incident["id"], "false_positive", note)

    def wait_quiet(self, since: datetime, env_entities: set[str]) -> bool:
        """Until no alert or telemetry gap about the environment is firing (bounded).

        Error-group signals are left out: they resolve on their own quiet timer, and
        a recurring one (noise) would otherwise hold every run to the limit. Any
        incident still active is closed before the next fault anyway.
        """

        def quiet() -> bool:
            return not any(
                s["status"] == "firing" and s["kind"] in ("prom_alert", "telemetry_gap")
                for s in self.environment_signals(since, env_entities)
            )

        return wait_for(quiet, self.cfg.cooldown_max_s, every_s=15)

    # ================================================================ run
    def run(self, scenario: Scenario, repeat: int, kind: str | None = None) -> dict[str, Any]:
        det = scenario.detection
        expected = metrics.Expected(
            expect_incident=det.expect_incident,
            deadline_s=det.deadline_s,
            entities=tuple((e.type, e.name) for e in det.entities),
            origin=(det.origin.type, det.origin.name) if det.origin else None,
            gap=(det.gap.classification, det.gap.severity) if det.gap else None,
            no_service_incident=det.no_service_incident,
        )
        record: dict[str, Any] = {
            "scenario": scenario.id,
            "repeat": repeat,
            "kind": kind or scenario.kind,
        }
        error: str | None = None
        started = now()
        incidents: list[metrics.IncidentObs] = []
        gaps: list[metrics.GapObs] = []
        signals: list[dict[str, Any]] = []
        fault_start = observe_end = started
        try:
            record["baseline_notes"] = self.baseline(scenario.load_users)
            env_entities = self.environment_entities()
            record["baseline_notes"] += self.clear_before_fault(env_entities)
            time.sleep(self.cfg.settle_s)
            fault_start = now()
            t0 = time.monotonic()
            record["inject"] = self.run_steps(list(scenario.inject), t0)
            remaining = t0 + det.observe_s - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            observe_end = now()
            env_entities |= self.environment_entities()
            incidents = self.observe_incidents(fault_start, env_entities)
            gaps = self.observe_gaps(fault_start, observe_end)
            signals = [
                {"kind": s["kind"], "title": s["title"], "severity": s["severity"],
                 "starts_at": s["starts_at"], "alertname": s.get("labels", {}).get("alertname")}
                for s in self.environment_signals(fault_start, env_entities)
            ]  # fmt: skip
            if self.cfg.record_fixtures:
                record["fixtures"] = self.record_fixtures(
                    scenario, repeat, fault_start, observe_end
                )
            record["recover"] = self.run_steps(list(scenario.recover), time.monotonic())
            record["baseline_after"] = self.baseline(scenario.load_users)
            record["quiet_after"] = self.wait_quiet(fault_start, env_entities)
        except Exception as exc:  # recorded and the suite goes on; scored as invalid
            error = f"{type(exc).__name__}: {str(exc)[:500]}"
            log.warning("eval_run_failed", scenario=scenario.id, repeat=repeat, error=error)
            try:
                record["recover"] = self.run_steps(list(scenario.recover), time.monotonic())
                self.baseline(scenario.load_users)
            except Exception as again:
                record["recover_error"] = f"{type(again).__name__}: {str(again)[:300]}"
        run = metrics.RunRecord(
            scenario=scenario.id, repeat=repeat, kind=record["kind"], expected=expected,
            fault_start=fault_start, observe_end=observe_end, incidents=incidents,
            gaps=gaps, signals=signals, harness_error=error,
        )  # fmt: skip
        score = metrics.score_run(run)
        tps = (
            {i.id for i in incidents if i.in_environment and metrics.involves(i, expected.entities)}
            if expected.expect_incident
            else set()
        )
        try:
            self.close_incidents(started, tps, f"evaluation {scenario.id} run {repeat}")
        except Exception as exc:
            record["close_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        record |= {
            "started_at": started.isoformat(),
            "fault_start": fault_start.isoformat(),
            "observe_end": observe_end.isoformat(),
            "ended_at": now().isoformat(),
            "harness_error": error,
            "incidents": [_incident_json(i) for i in incidents],
            "gaps": [{**asdict(g), "started_at": g.started_at.isoformat()} for g in gaps],
            "signals": signals,
            "score": asdict(score),
        }
        return record

    # ============================================================ fixtures
    def record_fixtures(
        self, scenario: Scenario, repeat: int, start: datetime, end: datetime
    ) -> dict[str, Any]:
        """The telemetry around the fault, saved for replay (Step 17+ collectors)."""
        path = self.cfg.fixtures_dir / scenario.id / f"run{repeat}.json.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        window = TimeRange(start - timedelta(minutes=10), end)
        services = sorted(
            {
                *scenario.ground_truth.services,
                *(e.name for e in scenario.detection.entities if e.type == "service"),
            }
        )
        failed = asyncio.run(self._record(path, window, services))
        if failed:
            log.warning("eval_fixtures_partial", scenario=scenario.id, failed=failed)
        return {
            "path": str(path.relative_to(self.cfg.fixtures_dir.parent.parent)),
            "failed": failed,
        }

    async def _record(self, path: Path, window: TimeRange, services: list[str]) -> dict[str, str]:
        """Each store on its own: one that fails doesn't lose the others' responses."""
        adapter = FixtureTelemetryAdapter(path, Mode.RECORD)
        limits = Limits()
        s = self.settings
        failed: dict[str, str] = {}

        async def loki() -> None:
            async with LokiClient(s.loki_url, limits, transport=adapter.transport()) as client:
                for service in services:
                    selector = Selector(service=service, namespace=AVIRO_NAMESPACE)
                    await client.logs(selector, window, min_severity=17, limit=200)
                await client.logs(Selector(host=HOST_NAME), window, min_severity=17, limit=200)

        async def prometheus() -> None:
            async with PrometheusClient(
                s.prometheus_url, limits, transport=adapter.transport()
            ) as client:
                for service in services:
                    selector = Selector(service=service, namespace=AVIRO_NAMESPACE)
                    for template in (
                        Template.REQUEST_RATE,
                        Template.ERROR_RATIO,
                        Template.LATENCY_P95,
                    ):
                        await client.range(template, selector, window)

        async def jaeger() -> None:
            async with JaegerClient(s.jaeger_url, limits, transport=adapter.transport()) as client:
                for service in services:
                    await client.find(service, window, errors_only=True, limit=5)

        for name, store in (("loki", loki), ("prometheus", prometheus), ("jaeger", jaeger)):
            try:
                await store()
            except (TelemetryError, OSError) as exc:
                failed[name] = f"{type(exc).__name__}: {exc}"[:200]
        adapter.save()
        return failed


def _incident_json(i: metrics.IncidentObs) -> dict[str, Any]:
    return {
        "id": i.id, "ref": i.ref, "detected_at": i.detected_at.isoformat(),
        "started_at": i.started_at.isoformat(), "severity": i.severity, "status": i.status,
        "service_name": i.service_name, "in_environment": i.in_environment,
        "entities": [asdict(e) for e in i.entities],
        "primary_entity": asdict(i.primary_entity) if i.primary_entity else None,
        "env_gap": i.env_gap,
    }  # fmt: skip


def git_sha(repo: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip() or "unknown"


def runs_done(per_run: Path) -> set[tuple[str, int]]:
    if not per_run.exists():
        return set()
    done = set()
    for line in per_run.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            done.add((r["scenario"], int(r["repeat"])))
    return done
