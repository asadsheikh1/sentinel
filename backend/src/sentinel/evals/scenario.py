"""The scenario file (``evals/scenarios/<id>/scenario.yaml``): fault, load, ground truth.

A scenario says how to break AVIRO (``inject``), how to put it back
(``recover``), what detection should see (``detection``), and the ground truth
the AI steps are later scored against (``ground_truth``). Steps are a small,
closed vocabulary (:data:`Step`), so a scenario can't run arbitrary commands.
"""

from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

Category = Literal[
    "code_change",
    "config_change",
    "dependency_failure",
    "resource_exhaustion",
    "infrastructure",
    "data",
    "monitoring_gap",
    "none",
]
AVIRO_SERVICES = ("gateway", "inventory", "payments", "orders", "worker")
FAULT_SERVICES = ("gateway", "inventory", "payments", "orders")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Deploy(_Strict):
    """Deploy a git ref of AVIRO (``scripts/deploy.sh``), then post its deployment events."""

    step: Literal["deploy"] = "deploy"
    ref: str = Field(pattern=r"^[A-Za-z0-9._/-]{1,100}$")
    after_s: float = Field(default=0, ge=0)


class Fault(_Strict):
    """A runtime fault through AVIRO's admin API."""

    step: Literal["fault"] = "fault"
    service: Literal["gateway", "inventory", "payments", "orders"]
    type: Literal[
        "latency",
        "error_rate",
        "dependency_timeout",
        "memory_leak",
        "cpu_hog",
        "disk_fill",
        "process_crash",
    ]
    params: dict[str, str | int | float | bool] = Field(default_factory=dict)
    duration_s: float = Field(default=600, gt=0, le=3600)
    after_s: float = Field(default=0, ge=0)


class ClearFaults(_Strict):
    step: Literal["clear_faults"] = "clear_faults"
    after_s: float = Field(default=0, ge=0)


class Container(_Strict):
    """Stop or start one of AVIRO's containers (e.g. the Valkey broker)."""

    step: Literal["container"] = "container"
    name: Literal["valkey", "postgres", "worker"]
    action: Literal["stop", "start"]
    after_s: float = Field(default=0, ge=0)


class Data(_Strict):
    """Switch a scenario's data on (latent bug trigger) or off."""

    step: Literal["data"] = "data"
    name: Literal["s10"]
    action: Literal["apply", "remove"]
    after_s: float = Field(default=0, ge=0)


class DiskFill(_Strict):
    """Fill (or empty) the AVIRO host's data disk with a fill file."""

    step: Literal["disk_fill"] = "disk_fill"
    action: Literal["fill", "remove"]
    after_s: float = Field(default=0, ge=0)


class CpuHog(_Strict):
    """Busy loops on the AVIRO host itself (not inside a service)."""

    step: Literal["cpu_hog"] = "cpu_hog"
    action: Literal["start", "stop"]
    threads: int = Field(default=4, ge=1, le=16)
    after_s: float = Field(default=0, ge=0)


class Agent(_Strict):
    """Stop or start the SENTINEL Agent on the AVIRO host."""

    step: Literal["agent"] = "agent"
    action: Literal["stop", "start"]
    after_s: float = Field(default=0, ge=0)


class Host(_Strict):
    """Take the whole AVIRO host away (paused: no agent, no app, probes fail) and back."""

    step: Literal["host"] = "host"
    action: Literal["pause", "unpause"]
    after_s: float = Field(default=0, ge=0)


class Load(_Strict):
    """Change the number of synthetic shoppers."""

    step: Literal["load"] = "load"
    users: int = Field(ge=0, le=200)
    after_s: float = Field(default=0, ge=0)


Step = Annotated[
    Deploy | Fault | ClearFaults | Container | Data | DiskFill | CpuHog | Agent | Host | Load,
    Field(discriminator="step"),
]


class EntityMatch(_Strict):
    """An entity an incident should involve: a service or container by name, the host,
    or an endpoint (a probe target) by the host it probes."""

    type: Literal["service", "container", "host", "endpoint"]
    name: str = Field(min_length=1, max_length=100)


class GapExpectation(_Strict):
    classification: Literal["agent_problem", "host_or_network_down", "host_or_agent"]
    severity: Literal["info", "warning", "critical"]


MAX_OBSERVE_S = 12 * 3600


class Detection(_Strict):
    expect_incident: bool
    # Recall counts an incident only if it was opened within the deadline.
    # Up to 12 h: the soak is a scenario too.
    deadline_s: float = Field(default=300, gt=0, le=MAX_OBSERVE_S)
    # How long to watch after the fault (>= deadline): late detections are recorded too.
    observe_s: float = Field(default=420, gt=0, le=MAX_OBSERVE_S)
    # Any of these makes an incident a true positive (if it overlaps the fault window).
    entities: list[EntityMatch] = Field(default_factory=list)
    # Where the fault starts, when it isn't the first expected entity's service.
    origin: EntityMatch | None = None
    # Signals we expect to see (informational: which fired is recorded per run).
    signals: list[str] = Field(default_factory=list)
    # A telemetry gap is the expected detection (S15, S16).
    gap: GapExpectation | None = None
    # S15: the gap must not turn into a service incident.
    no_service_incident: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> "Detection":
        if self.observe_s < self.deadline_s:
            raise ValueError("observe_s must be >= deadline_s")
        if self.expect_incident and not self.entities and self.gap is None:
            raise ValueError("an expected incident needs entities (or a gap expectation)")
        return self


class Culprit(_Strict):
    ref: str | None = None  # the bad deploy's git ref (its commit SHA is resolved per run)
    file: str | None = None
    function: str | None = None


class GroundTruth(_Strict):
    """What the AI steps (Step 18+) are scored against. Not used by detection metrics."""

    category: Category
    services: list[str] = Field(default_factory=list)
    culprit: Culprit | None = None
    keywords: list[str] = Field(default_factory=list)
    canonical: str = Field(min_length=10, max_length=500)
    evidence_kinds: list[str] = Field(default_factory=list)
    repairable: bool = False
    repair_test: str | None = None


class Scenario(_Strict):
    id: str = Field(pattern=r"^S[0-9]{2}$")
    title: str = Field(min_length=5, max_length=200)
    kind: Literal["positive", "negative"]
    load_users: int = Field(default=10, ge=0, le=200)
    inject: list[Step] = Field(min_length=1)
    recover: list[Step] = Field(default_factory=list)
    detection: Detection
    ground_truth: GroundTruth
    notes: str = ""

    @model_validator(mode="after")
    def _negative_has_no_incident(self) -> "Scenario":
        if self.kind == "negative" and self.detection.expect_incident:
            raise ValueError("a negative scenario expects no incident")
        return self


def load(path: Path) -> Scenario:
    data: Any = yaml.safe_load(path.read_text())
    scenario = Scenario.model_validate(data)
    if path.parent.name != scenario.id:
        raise ValueError(f"{path}: the folder name must be the scenario id ({scenario.id})")
    return scenario


def load_suite(root: Path, ids: list[str] | None = None) -> list[Scenario]:
    """Every scenario under ``root`` (or the given ids), in id order."""
    scenarios = [load(p) for p in sorted(root.glob("S*/scenario.yaml"))]
    if ids:
        wanted = {i.upper() for i in ids}
        missing = wanted - {s.id for s in scenarios}
        if missing:
            raise ValueError(f"unknown scenarios: {', '.join(sorted(missing))}")
        scenarios = [s for s in scenarios if s.id in wanted]
    return scenarios
