"""The results folder: ``evals/results/<date>_<gitsha>_detection/``.

- ``config.json``: git SHA, hardware, settings, scenarios, repeats, environment timings.
- ``per_run.jsonl``: one line per run, appended as each run ends.
- ``metrics.json``: the suite metrics and the per-scenario breakdown.
- ``report.md``: the same, for people.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from sentinel.evals import metrics


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def load_runs(per_run: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in per_run.read_text().splitlines() if line.strip()]


def append_run(per_run: Path, record: dict[str, Any]) -> None:
    with per_run.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def scores_from(records: list[dict[str, Any]]) -> list[metrics.RunScore]:
    return [metrics.RunScore(**r["score"]) for r in records]


def build_metrics(folder: Path) -> dict[str, Any]:
    records = load_runs(folder / "per_run.jsonl")
    # A rerun of the same (scenario, repeat) replaces the earlier line.
    latest: dict[tuple[str, int], dict[str, Any]] = {}
    for r in records:
        latest[(r["scenario"], int(r["repeat"]))] = r
    scores = scores_from(list(latest.values()))
    config = json.loads((folder / "config.json").read_text())
    result = {
        "summary": metrics.summarise(scores),
        "per_scenario": metrics.per_scenario(scores),
        "integration_time_to_first_data_s": config.get("environment", {}).get(
            "time_to_first_data_s"
        ),
    }
    (folder / "metrics.json").write_text(json.dumps(result, indent=2) + "\n")
    (folder / "report.md").write_text(render_report(config, result, list(latest.values())))
    return result


def _fmt(value: Any, pct: bool = False) -> str:
    if value is None:
        return "-"
    if pct:
        return f"{value * 100:.0f}%"
    return str(value)


def render_report(
    config: dict[str, Any], result: dict[str, Any], runs: list[dict[str, Any]]
) -> str:
    s = result["summary"]
    ttd = s["ttd_s"]
    rr = s["recall_across_repeats"]
    lines = [
        "# Detection evaluation",
        "",
        f"- **Git SHA:** `{config.get('git_sha')}` · **AVIRO:** `{config.get('aviro_sha')}`",
        f"- **When:** {config.get('started_at')} → {config.get('ended_at', 'running')}",
        f"- **Machine:** {config.get('machine')}",
        f"- **Scenarios:** {', '.join(config.get('scenarios', []))} · **repeats:** {config.get('repeats')}",
        "- **Detection deadline:** per scenario (default 300 s); runs are watched for `observe_s`.",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Runs (invalid: harness error) | {s['runs']} ({s['invalid_runs']}) |",
        f"| Detection precision | {_fmt(s['precision'], True)} ({s['incidents_true_positive']} true, {s['incidents_false_positive']} false) |",
        f"| Detection recall (within deadline) | {_fmt(s['recall'], True)} · per repeat: mean {_fmt(rr['mean'], True)}, min {_fmt(rr['min'], True)}, max {_fmt(rr['max'], True)} |",
        f"| Detected, but after the deadline | {s['detected_after_deadline']} runs |",
        f"| Time to detect (median / p90 / max) | {_fmt(ttd['median'])} s / {_fmt(ttd['p90'])} s / {_fmt(ttd['max'])} s |",
        f"| False positives per hour (fault-free: {s['fault_free_hours']} h) | {_fmt(s['false_positives_per_hour'])} |",
        f"| Grouping accuracy (exactly one incident) | {_fmt(s['grouping_accuracy'], True)} |",
        f"| Origin accuracy (host scenarios) | {_fmt(s['origin_accuracy'], True)} |",
        f"| Gap classification accuracy | {_fmt(s['gap_classification_accuracy'], True)} |",
        f"| Agent gap without a service incident (S15) | {_fmt(s['gap_without_service_incident'], True)} |",
        f"| Incidents about other things on this SENTINEL (excluded) | {s['incidents_outside_environment']} |",
        "",
        "Integration time to first data (every required checklist item ticked): "
        + ", ".join(
            f"{k} {_fmt(v)} s"
            for k, v in (result.get("integration_time_to_first_data_s") or {}).items()
        ),
        "",
        "## Per scenario",
        "",
        "| Scenario | Detected (deadline) | TTD, s | Incidents (true / false) | One incident | Origin | Gap |",
        "|---|---|---|---|---|---|---|",
    ]
    for sid, p in result["per_scenario"].items():
        n = p["runs"]
        lines.append(
            f"| {sid} | {p['detected']}/{n} | {', '.join(str(t) for t in p['ttd_s']) or '-'} "
            f"| {p['true_positives']} / {p['false_positives']} | {p['grouping_correct']}/{n} "
            f"| {_fmt(p['origin_correct'])} | {_fmt(p['gap_correct'])} |"
            + (f" ⚠ {p['invalid_runs']} invalid" if p["invalid_runs"] else "")
        )
    lines += [
        "",
        "## Runs",
        "",
        "| Scenario | Repeat | Fault start | Incidents | Signals seen | Notes |",
        "|---|---|---|---|---|---|",
    ]
    for r in sorted(runs, key=lambda r: (r["scenario"], r["repeat"])):
        incidents = (
            "; ".join(
                f"{i['ref']} {i['severity']} {'+' if i['in_environment'] else 'other'} "
                f"({', '.join(e['display_name'] for e in i['entities'])})"
                for i in r["incidents"]
            )
            or "none"
        )
        names = sorted({sig.get("alertname") or sig["kind"] for sig in r.get("signals", [])})
        notes = r.get("harness_error") or "; ".join(r.get("baseline_notes", []))
        lines.append(
            f"| {r['scenario']} | {r['repeat']} | {r['fault_start'][11:19]} | {incidents} "
            f"| {', '.join(names) or '-'} | {notes or ''} |"
        )
    return "\n".join(lines) + "\n"
