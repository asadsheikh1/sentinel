"""Evaluation harness (Step 16): metric functions on synthetic ground truth, scenario files."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from sentinel.evals import metrics, results
from sentinel.evals.metrics import EntityObs, Expected, GapObs, IncidentObs, RunRecord
from sentinel.evals.scenario import Scenario, load, load_suite

ROOT = Path(__file__).resolve().parents[3]
SCENARIOS = ROOT / "evals" / "scenarios"
T0 = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)

ORDERS = EntityObs("service", "aviro/orders@dev", "orders")
GATEWAY = EntityObs("service", "aviro/gateway@dev", "gateway")
PAYMENTS = EntityObs("service", "aviro/payments@dev", "payments")
HOST = EntityObs("host", "aviro-host", "aviro-host")
VALKEY = EntityObs("container", "abc123", "aviro_valkey_1")


def incident(
    at_s: float,
    *entities: EntityObs,
    primary: EntityObs | None = None,
    in_env: bool = True,
    ident: str | None = None,
) -> IncidentObs:
    when = T0 + timedelta(seconds=at_s)
    return IncidentObs(
        id=ident or f"i-{at_s}-{len(entities)}",
        ref=f"INC-{int(at_s)}",
        detected_at=when,
        started_at=when,
        severity="SEV2",
        status="open",
        service_name=None,
        entities=entities,
        primary_entity=primary or (entities[0] if entities else None),
        in_environment=in_env,
    )


def run(
    *incidents: IncidentObs,
    expect: bool = True,
    entities: tuple[tuple[str, str], ...] = (("service", "orders"), ("service", "gateway")),
    kind: str = "positive",
    observe_s: float = 420,
    deadline_s: float = 300,
    repeat: int = 1,
    scenario: str = "S01",
    **extra: Any,
) -> RunRecord:
    return RunRecord(
        scenario=scenario,
        repeat=repeat,
        kind=kind,
        expected=Expected(
            expect_incident=expect, deadline_s=deadline_s, entities=entities, **extra
        ),
        fault_start=T0,
        observe_end=T0 + timedelta(seconds=observe_s),
        incidents=list(incidents),
    )


# ------------------------------------------------------------------ entity matching


@pytest.mark.parametrize(
    ("entity", "want", "match"),
    [
        (ORDERS, ("service", "orders"), True),
        (ORDERS, ("service", "Orders"), True),
        (ORDERS, ("service", "order"), False),
        (ORDERS, ("host", "orders"), False),
        (HOST, ("host", "aviro-host"), True),
        (
            EntityObs("host", "aviro-host.example.com", "aviro-host.example.com"),
            ("host", "aviro-host"),
            True,
        ),
        (VALKEY, ("container", "valkey"), True),
        (VALKEY, ("container", "valkey-2"), False),
        (
            EntityObs("endpoint", "http://aviro-host:8081/api/health", "eval-check"),
            ("endpoint", "aviro-host"),
            True,
        ),
        (
            EntityObs("endpoint", "http://shop.example.com/", "check"),
            ("endpoint", "aviro-host"),
            False,
        ),
    ],
)
def test_entity_matches(entity: EntityObs, want: tuple[str, str], match: bool) -> None:
    assert metrics.entity_matches(entity, want) is match


# ------------------------------------------------------------------------ run scores


def test_detected_within_the_deadline() -> None:
    score = metrics.score_run(run(incident(95, ORDERS, GATEWAY)))
    assert score.detected is True
    assert score.ttd_s == 95
    assert (score.true_positives, score.false_positives) == (1, 0)
    assert score.grouping_correct is True


def test_detected_after_the_deadline_is_not_recall_but_is_timed() -> None:
    score = metrics.score_run(run(incident(360, ORDERS)))
    assert score.detected is False
    assert score.ttd_s == 360


def test_missed() -> None:
    score = metrics.score_run(run())
    assert score.detected is False
    assert score.ttd_s is None
    assert score.grouping_correct is False


def test_incident_about_the_wrong_thing_is_a_false_positive() -> None:
    score = metrics.score_run(run(incident(60, PAYMENTS)))
    assert (score.true_positives, score.false_positives) == (0, 1)
    assert score.detected is False


def test_two_incidents_for_one_fault_is_a_grouping_error() -> None:
    score = metrics.score_run(run(incident(60, ORDERS), incident(90, GATEWAY)))
    assert score.true_positives == 2
    assert score.detected is True
    assert score.ttd_s == 60  # the first one
    assert score.grouping_correct is False


def test_an_extra_unrelated_incident_also_breaks_grouping() -> None:
    score = metrics.score_run(run(incident(60, ORDERS), incident(90, PAYMENTS)))
    assert (score.true_positives, score.false_positives) == (1, 1)
    assert score.grouping_correct is False


def test_incidents_outside_the_window_or_environment_are_ignored() -> None:
    score = metrics.score_run(
        run(
            incident(-30, ORDERS),  # before the fault
            incident(500, ORDERS),  # after the observation window
            incident(60, PAYMENTS, in_env=False),  # another system on this SENTINEL
        )
    )
    assert (score.true_positives, score.false_positives, score.outside_environment) == (0, 0, 1)


def test_negative_scenario() -> None:
    clean = metrics.score_run(run(expect=False, kind="negative", entities=()))
    assert clean.detected is None
    assert clean.grouping_correct is None
    assert clean.false_positives == 0
    noisy = metrics.score_run(
        run(incident(100, ORDERS), expect=False, kind="negative", entities=())
    )
    assert noisy.false_positives == 1


def test_origin() -> None:
    host_first = run(
        incident(100, HOST, ORDERS, primary=HOST),
        entities=(("host", "aviro-host"), ("service", "orders")),
        origin=("host", "aviro-host"),
    )
    assert metrics.score_run(host_first).origin_correct is True
    service_first = run(
        incident(100, HOST, ORDERS, primary=ORDERS),
        entities=(("host", "aviro-host"), ("service", "orders")),
        origin=("host", "aviro-host"),
    )
    assert metrics.score_run(service_first).origin_correct is False


def test_gap_classification_and_no_service_incident() -> None:
    record = run(expect=False, entities=(), gap=("agent_problem", "info"), no_service_incident=True)
    record.gaps = [GapObs("agent_problem", "info", T0 + timedelta(seconds=300))]
    score = metrics.score_run(record)
    assert score.gap_correct is True
    assert score.no_service_incident_ok is True

    wrong = run(
        incident(320, ORDERS),
        expect=False,
        entities=(),
        gap=("agent_problem", "info"),
        no_service_incident=True,
    )
    wrong.gaps = [GapObs("host_or_network_down", "critical", T0)]
    score = metrics.score_run(wrong)
    assert score.gap_correct is False
    assert score.no_service_incident_ok is False


def test_a_gap_incident_without_entities_counts_in_a_gap_scenario() -> None:
    gap_incident = IncidentObs(
        id="g", ref="INC-9", detected_at=T0 + timedelta(seconds=300),
        started_at=T0 + timedelta(seconds=300), severity="SEV2", status="open",
        service_name=None, entities=(), env_gap=True,
    )  # fmt: skip
    host_down = run(
        gap_incident,
        entities=(("host", "aviro-host"),),
        gap=("host_or_network_down", "critical"),
        deadline_s=480,
        observe_s=540,
    )
    score = metrics.score_run(host_down)
    assert (score.true_positives, score.detected, score.ttd_s) == (1, True, 300)
    # Outside a gap scenario the same incident is not a true positive.
    assert metrics.score_run(run(gap_incident)).true_positives == 0


def test_harness_error_makes_a_run_invalid() -> None:
    record = run(incident(60, ORDERS))
    record.harness_error = "HostError: boom"
    assert metrics.score_run(record).valid is False


# ------------------------------------------------------------------------ summary


def test_summary_on_synthetic_ground_truth() -> None:
    scores = [
        metrics.score_run(run(incident(60, ORDERS), repeat=1)),  # detected, 60 s
        metrics.score_run(run(incident(120, ORDERS), incident(130, PAYMENTS), repeat=2)),  # 1 FP
        metrics.score_run(run(repeat=3)),  # missed
        metrics.score_run(run(incident(400, ORDERS), repeat=4, observe_s=600)),  # late
        metrics.score_run(  # fault-free hour with one false positive
            run(incident(100, ORDERS), expect=False, kind="soak", entities=(), observe_s=3600)
        ),
    ]
    s = metrics.summarise(scores)
    assert s["incidents_true_positive"] == 3
    assert s["incidents_false_positive"] == 2
    assert s["precision"] == 0.6
    assert s["recall"] == 0.5  # 2 of the 4 runs expecting an incident, within the deadline
    assert s["recall_across_repeats"] == {"mean": 0.5, "min": 0.0, "max": 1.0}
    assert s["detected_after_deadline"] == 1
    assert s["ttd_s"] == {"median": 90.0, "p90": 120, "max": 120}
    assert s["false_positives_per_hour"] == 1.0
    assert s["fault_free_hours"] == 1.0
    assert s["grouping_accuracy"] == 0.5  # runs 1 and 4 had exactly one incident


def test_summary_counts_invalid_runs_without_scoring_them() -> None:
    bad = run(incident(60, ORDERS))
    bad.harness_error = "x"
    s = metrics.summarise([metrics.score_run(bad), metrics.score_run(run(incident(30, ORDERS)))])
    assert (s["runs"], s["invalid_runs"], s["recall"]) == (2, 1, 1.0)


def test_summary_of_nothing() -> None:
    s = metrics.summarise([])
    assert s["precision"] is None
    assert s["recall"] is None
    assert s["ttd_s"]["median"] is None


def test_percentile() -> None:
    assert metrics.percentile([], 90) is None
    assert metrics.percentile([5], 90) == 5
    assert metrics.percentile(list(range(1, 11)), 90) == 9
    assert metrics.percentile(list(range(1, 11)), 100) == 10


def test_per_scenario() -> None:
    scores = [
        metrics.score_run(run(incident(60, ORDERS), repeat=1)),
        metrics.score_run(run(repeat=2)),
        metrics.score_run(
            run(
                incident(200, HOST, primary=HOST),
                scenario="S13",
                entities=(("host", "aviro-host"),),
                origin=("host", "aviro-host"),
            )
        ),
    ]
    p = metrics.per_scenario(scores)
    assert p["S01"]["detected"] == 1
    assert p["S01"]["ttd_s"] == [60.0]
    assert p["S01"]["origin_correct"] is None
    assert p["S13"]["origin_correct"] == 1


# ------------------------------------------------------------------- results folder


def test_results_folder_round_trip(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text(
        json.dumps({"git_sha": "abc", "scenarios": ["S01"], "repeats": 1,
                    "environment": {"time_to_first_data_s": {"host": 42.0}}})
    )  # fmt: skip
    from dataclasses import asdict

    score = asdict(metrics.score_run(run(incident(60, ORDERS))))
    record: dict[str, Any] = {
        "scenario": "S01", "repeat": 1, "kind": "positive",
        "fault_start": T0.isoformat(), "observe_end": T0.isoformat(),
        "incidents": [], "signals": [], "harness_error": None,
        "score": score,
    }  # fmt: skip
    results.append_run(tmp_path / "per_run.jsonl", {**record, "score": {**score, "ttd_s": 999}})
    results.append_run(tmp_path / "per_run.jsonl", record)  # a rerun replaces the first
    out = results.build_metrics(tmp_path)
    assert out["summary"]["ttd_s"]["median"] == 60
    assert out["integration_time_to_first_data_s"] == {"host": 42.0}
    report = (tmp_path / "report.md").read_text()
    assert "| S01 | 1/1 | 60.0 |" in report
    assert json.loads((tmp_path / "metrics.json").read_text())["summary"]["runs"] == 1


# ------------------------------------------------------------------- scenario files


def test_the_suite_has_s01_to_s16() -> None:
    suite = load_suite(SCENARIOS)
    assert [s.id for s in suite] == [f"S{n:02d}" for n in range(1, 17)]


def test_every_scenario_can_be_recovered_or_needs_no_recovery() -> None:
    for scenario in load_suite(SCENARIOS):
        if scenario.kind == "positive" and scenario.id not in ("S12",):
            assert scenario.recover, f"{scenario.id} has no recovery steps"


def test_code_scenarios_name_their_culprit() -> None:
    for scenario in load_suite(SCENARIOS):
        if any(step.step == "deploy" for step in scenario.inject):
            assert scenario.ground_truth.culprit is not None
            assert scenario.ground_truth.culprit.ref


def test_subset_and_unknown_ids() -> None:
    assert [s.id for s in load_suite(SCENARIOS, ["s05", "S01"])] == ["S01", "S05"]
    with pytest.raises(ValueError, match="S99"):
        load_suite(SCENARIOS, ["S99"])


def _minimal(**over: Any) -> dict[str, Any]:
    base = {
        "id": "S01",
        "title": "A scenario",
        "kind": "positive",
        "inject": [{"step": "fault", "service": "payments", "type": "latency"}],
        "detection": {
            "expect_incident": True,
            "entities": [{"type": "service", "name": "payments"}],
        },
        "ground_truth": {"category": "dependency_failure", "canonical": "Payments is slow."},
    }
    return {**base, **over}


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"kind": "negative"}, "negative scenario expects no incident"),
        ({"detection": {"expect_incident": True}}, "needs entities"),
        (
            {"detection": {"expect_incident": False, "deadline_s": 600, "observe_s": 300}},
            "observe_s",
        ),
        ({"inject": [{"step": "shell", "cmd": "rm -rf /"}]}, "step"),
        ({"inject": [{"step": "deploy", "ref": "main; rm -rf /"}]}, "ref"),
        ({"inject": [{"step": "fault", "service": "db", "type": "latency"}]}, "service"),
        ({"surprise": 1}, "surprise"),
    ],
)
def test_bad_scenarios_are_refused(over: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Scenario.model_validate(_minimal(**over))


def test_folder_must_match_the_id(tmp_path: Path) -> None:
    import yaml

    folder = tmp_path / "S02"
    folder.mkdir()
    (folder / "scenario.yaml").write_text(yaml.safe_dump(_minimal()))
    with pytest.raises(ValueError, match="folder name"):
        load(folder / "scenario.yaml")
