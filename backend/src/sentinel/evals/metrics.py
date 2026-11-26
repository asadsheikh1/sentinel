"""Detection metrics (testing and evaluation design §3). Pure functions over recorded runs.

A run records what SENTINEL did while one scenario's fault was active: the
incidents it opened (with their entities), the telemetry gaps it raised, and when.
Definitions:

- **True-positive incident:** opened within the run's observation window, and
  involving one of the scenario's expected entities (in a gap scenario, also an
  incident carrying the environment's telemetry-gap signal).
- **False positive:** any other incident opened in the window that involves the
  evaluation environment (AVIRO and its host). Incidents about anything else on
  the SENTINEL instance are counted separately and excluded.
- **Detected:** a true positive opened within the deadline.
- **Time to detect:** the first true positive's ``detected_at`` minus the fault start.
- **Grouping correct:** exactly one incident about the environment during the fault, and
  it is a true positive.
- **False-positive rate:** false positives per hour of fault-free observation
  (negative scenarios and the soak).
"""

import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class EntityObs:
    type: str
    key: str
    display_name: str


@dataclass(frozen=True)
class IncidentObs:
    id: str
    ref: str
    detected_at: datetime
    started_at: datetime
    severity: str
    status: str
    service_name: str | None
    entities: tuple[EntityObs, ...]
    primary_entity: EntityObs | None = None
    in_environment: bool = True  # involves AVIRO or its host
    # Carries a telemetry-gap signal of this environment's integrations (gap
    # signals have no entity, so such an incident may have none).
    env_gap: bool = False


@dataclass(frozen=True)
class GapObs:
    classification: str
    severity: str
    started_at: datetime


@dataclass(frozen=True)
class Expected:
    expect_incident: bool
    deadline_s: float
    entities: tuple[tuple[str, str], ...]  # (type, name)
    origin: tuple[str, str] | None = None
    gap: tuple[str, str] | None = None  # (classification, severity)
    no_service_incident: bool = False


@dataclass
class RunRecord:
    scenario: str
    repeat: int
    kind: str  # positive | negative | soak
    expected: Expected
    fault_start: datetime
    observe_end: datetime
    incidents: list[IncidentObs] = field(default_factory=list)
    gaps: list[GapObs] = field(default_factory=list)
    signals: list[dict[str, Any]] = field(default_factory=list)
    harness_error: str | None = None

    @property
    def observed_hours(self) -> float:
        return max(0.0, (self.observe_end - self.fault_start).total_seconds() / 3600)


def entity_matches(entity: EntityObs, want: tuple[str, str]) -> bool:
    """A service matches by name (display name or the name part of its key); a host by name."""
    kind, name = want
    if entity.type != kind:
        return False
    name = name.lower()
    candidates = {entity.display_name.lower(), entity.key.lower()}
    if kind == "service":  # key: namespace/name@environment
        candidates.add(entity.key.split("/", 1)[-1].split("@", 1)[0].lower())
    elif kind == "host":  # also without a domain
        candidates.add(entity.key.split(".", 1)[0].lower())
    elif kind == "endpoint":  # a probe target: matched by the host it probes
        candidates.add(urlsplit(entity.key).hostname or entity.key.split(":", 1)[0].lower())
    else:  # container: compose names it <project>_<service>_<n>
        candidates.add(entity.display_name.lower().removeprefix("aviro_").rsplit("_", 1)[0])
    return name in candidates


def involves(incident: IncidentObs, wanted: Iterable[tuple[str, str]]) -> bool:
    wanted = list(wanted)
    return any(entity_matches(e, w) for e in incident.entities for w in wanted)


@dataclass(frozen=True)
class RunScore:
    scenario: str
    repeat: int
    kind: str
    valid: bool
    expect_incident: bool
    true_positives: int
    false_positives: int
    outside_environment: int
    detected: bool | None  # None when no incident is expected
    ttd_s: float | None  # the first true positive, even after the deadline
    grouping_correct: bool | None
    origin_correct: bool | None
    gap_correct: bool | None
    no_service_incident_ok: bool | None
    observed_hours: float


def score_run(run: RunRecord) -> RunScore:
    exp = run.expected
    in_window = [i for i in run.incidents if run.fault_start <= i.detected_at <= run.observe_end]
    outside = [i for i in in_window if not i.in_environment]
    inside = [i for i in in_window if i.in_environment]
    tps = [
        i
        for i in inside
        if exp.expect_incident
        and (involves(i, exp.entities) or (exp.gap is not None and i.env_gap))
    ]
    fps = [i for i in inside if i not in tps]
    first = min(tps, key=lambda i: i.detected_at) if tps else None
    ttd = (first.detected_at - run.fault_start).total_seconds() if first else None
    detected = (ttd is not None and ttd <= exp.deadline_s) if exp.expect_incident else None
    # One fault, one incident: a true positive and nothing else about this environment.
    grouping = (len(tps) == 1 and len(inside) == 1) if exp.expect_incident else None
    origin = None
    if exp.origin is not None:
        origin = bool(
            first and first.primary_entity and entity_matches(first.primary_entity, exp.origin)
        )
    gap_ok = None
    if exp.gap is not None:
        gap_ok = any((g.classification, g.severity) == exp.gap for g in run.gaps)
    no_service = None
    if exp.no_service_incident:
        no_service = not any(e.type == "service" for i in inside for e in i.entities)
    return RunScore(
        scenario=run.scenario,
        repeat=run.repeat,
        kind=run.kind,
        valid=run.harness_error is None,
        expect_incident=exp.expect_incident,
        true_positives=len(tps),
        false_positives=len(fps),
        outside_environment=len(outside),
        detected=detected,
        ttd_s=ttd,
        grouping_correct=grouping,
        origin_correct=origin,
        gap_correct=gap_ok,
        no_service_incident_ok=no_service,
        observed_hours=run.observed_hours,
    )


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..100); None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _mean_min_max(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "min": None, "max": None}
    return {
        "mean": round(statistics.fmean(values), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def summarise(scores: Sequence[RunScore]) -> dict[str, Any]:
    """The suite's detection metrics; invalid runs (harness errors) are counted, not scored."""
    valid = [s for s in scores if s.valid]
    tp = sum(s.true_positives for s in valid)
    fp = sum(s.false_positives for s in valid)
    expecting = [s for s in valid if s.expect_incident]
    detected = [s for s in expecting if s.detected]
    ttds = [s.ttd_s for s in expecting if s.detected and s.ttd_s is not None]
    late = [s for s in expecting if not s.detected and s.ttd_s is not None]
    fault_free = [s for s in valid if s.kind in ("negative", "soak")]
    ff_hours = sum(s.observed_hours for s in fault_free)
    ff_fp = sum(s.false_positives for s in fault_free)
    grouping = [s for s in expecting if s.grouping_correct is not None]
    gaps = [s for s in valid if s.gap_correct is not None]
    origins = [s for s in valid if s.origin_correct is not None]
    no_service = [s for s in valid if s.no_service_incident_ok is not None]

    # Per-repeat recall, so the report can show a range across repeats.
    by_repeat: dict[int, list[RunScore]] = {}
    for s in expecting:
        by_repeat.setdefault(s.repeat, []).append(s)
    recall_per_repeat = [
        sum(bool(s.detected) for s in runs) / len(runs) for runs in by_repeat.values() if runs
    ]

    return {
        "runs": len(scores),
        "invalid_runs": len(scores) - len(valid),
        "incidents_true_positive": tp,
        "incidents_false_positive": fp,
        "incidents_outside_environment": sum(s.outside_environment for s in valid),
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(len(detected), len(expecting)),
        "recall_across_repeats": _mean_min_max(recall_per_repeat),
        "detected_after_deadline": len(late),
        "ttd_s": {
            "median": round(statistics.median(ttds), 1) if ttds else None,
            "p90": percentile(ttds, 90),
            "max": max(ttds) if ttds else None,
        },
        "false_positives_per_hour": round(ff_fp / ff_hours, 3) if ff_hours else None,
        "fault_free_hours": round(ff_hours, 3),
        "grouping_accuracy": _ratio(sum(bool(s.grouping_correct) for s in grouping), len(grouping)),
        "origin_accuracy": _ratio(sum(bool(s.origin_correct) for s in origins), len(origins)),
        "gap_classification_accuracy": _ratio(sum(bool(s.gap_correct) for s in gaps), len(gaps)),
        "gap_without_service_incident": _ratio(
            sum(bool(s.no_service_incident_ok) for s in no_service), len(no_service)
        ),
    }


def per_scenario(scores: Sequence[RunScore]) -> dict[str, dict[str, Any]]:
    """Each scenario across its repeats: detected n/N, TTDs, incidents, grouping, gaps."""
    out: dict[str, dict[str, Any]] = {}
    for scenario in sorted({s.scenario for s in scores}):
        runs = [s for s in scores if s.scenario == scenario and s.valid]
        ttds = [s.ttd_s for s in runs if s.ttd_s is not None]
        out[scenario] = {
            "runs": len(runs),
            "invalid_runs": sum(1 for s in scores if s.scenario == scenario and not s.valid),
            "detected": sum(bool(s.detected) for s in runs),
            "ttd_s": sorted(round(t, 1) for t in ttds),
            "true_positives": [s.true_positives for s in runs],
            "false_positives": [s.false_positives for s in runs],
            "grouping_correct": sum(bool(s.grouping_correct) for s in runs),
            "origin_correct": sum(bool(s.origin_correct) for s in runs)
            if any(s.origin_correct is not None for s in runs)
            else None,
            "gap_correct": sum(bool(s.gap_correct) for s in runs)
            if any(s.gap_correct is not None for s in runs)
            else None,
        }
    return out
