"""``sentinel eval …``: run the detection suite and write its results.

    sentinel eval run --repeats 3                 # every scenario, a new results folder
    sentinel eval run --scenarios S01 S05 --repeats 1
    sentinel eval run --resume evals/results/<folder>   # skip runs already recorded
    sentinel eval soak --hours 2 --resume evals/results/<folder>
    sentinel eval report evals/results/<folder>   # recompute metrics.json and report.md

Credentials of a SENTINEL responder (or admin): SENTINEL_EVAL_EMAIL and
SENTINEL_EVAL_PASSWORD. The stack (`make stack-up`) must be running.
"""

import argparse
import json
import os
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sentinel.config import Settings


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def add_parser(sub: Any) -> None:
    ev = sub.add_parser("eval", help="the evaluation suite (detection, from Step 16)")
    ev_sub = ev.add_subparsers(dest="eval_command", required=True)
    run = ev_sub.add_parser("run", help="run scenarios against the live stack")
    run.add_argument("--scenarios", nargs="*", help="ids (default: all)")
    run.add_argument("--repeats", type=int, default=3)
    run.add_argument("--resume", type=Path, help="an existing results folder to continue")
    run.add_argument("--no-fixtures", action="store_true")
    run.add_argument("--keep-host", action="store_true", help="leave the AVIRO host running")
    soak = ev_sub.add_parser("soak", help="fault-free load for the false-positive rate")
    soak.add_argument("--hours", type=float, default=2)
    soak.add_argument("--resume", type=Path, required=True)
    report = ev_sub.add_parser("report", help="recompute metrics.json and report.md")
    report.add_argument("folder", type=Path)


def _config(args: argparse.Namespace) -> Any:
    from sentinel.evals.runner import SuiteConfig

    root = repo_root()
    email = os.environ.get("SENTINEL_EVAL_EMAIL")
    password = os.environ.get("SENTINEL_EVAL_PASSWORD")
    if not email or not password:
        raise SystemExit("set SENTINEL_EVAL_EMAIL and SENTINEL_EVAL_PASSWORD")
    return SuiteConfig(
        api_url=os.environ.get("SENTINEL_EVAL_API", "http://127.0.0.1:8000"),
        email=email,
        password=password,
        scenarios_dir=root / "evals" / "scenarios",
        results_dir=root / "evals" / "results",
        fixtures_dir=root / "evals" / "fixtures",
        host_script=root / "evals" / "host" / "aviro-host.sh",
        locustfile=_locustfile(root),
        record_fixtures=not getattr(args, "no_fixtures", False),
    )


def _locustfile(root: Path) -> Path:
    """AVIRO's locustfile at the ref the host runs (AVIRO_REF), copied where Podman can mount it."""
    ref = os.environ.get("AVIRO_REF", "main")
    out = subprocess.run(
        ["git", "-C", str(root.parent / "aviro-shop"), "show", f"{ref}:loadgen/locustfile.py"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    path = root / "evals" / ".work" / "locustfile.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(out.stdout)
    return path


def _machine() -> str:
    def sysctl(name: str) -> str:
        out = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, check=False)
        return out.stdout.strip()

    chip = sysctl("machdep.cpu.brand_string") or platform.processor()
    mem = sysctl("hw.memsize")
    gb = f", {round(int(mem) / 2**30)} GB" if mem.isdigit() else ""
    return f"{chip}{gb}, {platform.system()} {platform.release()}"


def run_eval(args: argparse.Namespace) -> int:
    from sentinel.evals import results
    from sentinel.evals.runner import Runner, git_sha, runs_done
    from sentinel.evals.scenario import load_suite

    cfg = _config(args)
    settings = Settings()
    scenarios = load_suite(cfg.scenarios_dir, args.scenarios)
    root = repo_root()
    if args.resume:
        folder = args.resume
        config = json.loads((folder / "config.json").read_text())
    else:
        stamp = datetime.now(UTC).strftime("%Y-%m-%d")
        folder = cfg.results_dir / f"{stamp}_{git_sha(root)[:8]}_detection"
        folder.mkdir(parents=True, exist_ok=True)
        config = {
            "suite": "detection",
            "git_sha": git_sha(root),
            "aviro_sha": git_sha(root.parent / "aviro-shop"),
            "machine": _machine(),
            "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "scenarios": [s.id for s in scenarios],
            "repeats": args.repeats,
            "settings": {
                "correlation_window_s": settings.correlation_window_s,
                "mitigate_after_s": settings.mitigate_after_s,
                "error_poll_interval_s": settings.error_poll_interval_s,
            },
        }
    per_run = folder / "per_run.jsonl"
    done = runs_done(per_run)
    runner = Runner(cfg, settings)
    try:
        env = runner.setup()
        config.setdefault("environments", []).append(
            {"time_to_first_data_s": env.time_to_first_data_s,
             "at": datetime.now(UTC).isoformat(timespec="seconds")}
        )  # fmt: skip
        config["environment"] = config["environments"][0]
        (folder / "config.json").write_text(json.dumps(config, indent=2) + "\n")
        for repeat in range(1, int(config["repeats"]) + 1):
            for scenario in scenarios:
                if (scenario.id, repeat) in done:
                    continue
                print(f"== {scenario.id} run {repeat}: {scenario.title}", flush=True)
                record = runner.run(scenario, repeat)
                results.append_run(per_run, record)
                score = record["score"]
                print(
                    f"   detected={score['detected']} ttd={score['ttd_s']} "
                    f"tp={score['true_positives']} fp={score['false_positives']} "
                    f"error={record['harness_error']}",
                    flush=True,
                )
                results.build_metrics(folder)
    finally:
        config["ended_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        (folder / "config.json").write_text(json.dumps(config, indent=2) + "\n")
        if (folder / "per_run.jsonl").exists():
            results.build_metrics(folder)
        if not args.keep_host:
            runner.teardown()
        runner.api.close()
    print(f"Results: {folder}")
    return 0


def run_soak(args: argparse.Namespace) -> int:
    from sentinel.evals import results
    from sentinel.evals.runner import Runner
    from sentinel.evals.scenario import Scenario

    cfg = _config(args)
    folder = args.resume
    soak = Scenario.model_validate(
        {
            "id": "S00",
            "title": "Soak: normal load, no faults",
            "kind": "negative",
            "inject": [{"step": "load", "users": 10}],
            "detection": {
                "expect_incident": False,
                "deadline_s": args.hours * 3600,
                "observe_s": args.hours * 3600,
            },
            "ground_truth": {
                "category": "none",
                "canonical": "No fault: nothing should be detected.",
            },
        }
    )
    runner = Runner(cfg, Settings())
    try:
        env = runner.setup()
        print(f"== soak for {args.hours} h", flush=True)
        record = runner.run(soak, 1, kind="soak")
        record["environment"] = {"time_to_first_data_s": env.time_to_first_data_s}
        results.append_run(folder / "per_run.jsonl", record)
        results.build_metrics(folder)
    finally:
        runner.teardown()
        runner.api.close()
    return 0


def run_report(args: argparse.Namespace) -> int:
    from sentinel.evals import results

    out = results.build_metrics(args.folder)
    print(json.dumps(out["summary"], indent=2))
    return 0


def dispatch(args: argparse.Namespace) -> int:
    handlers = {"run": run_eval, "soak": run_soak, "report": run_report}
    return handlers[args.eval_command](args)
