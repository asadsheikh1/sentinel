# Evaluation suite

How well SENTINEL detects real faults in a real system, measured, not claimed ([testing and evaluation design](../docs/3-quality/1-testing-and-evaluation.md)). Step 16 measures **detection**. The AI steps (root cause, from Step 18) are scored on the same scenarios later, with the `ground_truth` already in each file.

| Folder | What |
|---|---|
| `scenarios/S01…S16/scenario.yaml` | One fault each: how to inject it, how to recover, what detection should see, and the ground truth for root cause. Schema: [`sentinel/evals/scenario.py`](../backend/src/sentinel/evals/scenario.py) |
| `host/` | The **AVIRO host**: a disposable Ubuntu 24.04 machine running AVIRO in its own container engine, on its own disk |
| `results/<date>_<sha>_detection/` | Committed results. `config.json` (git SHA, machine, settings), `per_run.jsonl` (every run), `metrics.json`, `report.md` |
| `fixtures/<scenario>/run<n>.json` | The telemetry around each fault (Loki, Prometheus, Jaeger responses), recorded for replay by later steps |

## The environment

Each suite run builds it from scratch, the way a user would onboard a machine:

1. **The AVIRO host** (`host/aviro-host.sh`).
   - A systemd machine on SENTINEL's ingest network.
   - AVIRO runs inside it with its own Podman, deployed with AVIRO's own `scripts/deploy.sh`.
   - Its `/var/lib/containers` (AVIRO's images, Postgres and Valkey data) is a separate 6 GiB ext4 filesystem. S13 fills that disk, and SENTINEL's stores are on a different one.
   - An edge proxy publishes AVIRO on the host's port 8081.
2. **Three integrations**, created through SENTINEL's API:
   - **an app**, for AVIRO's OpenTelemetry;
   - **a container host**, whose agent is installed with the real installer (the token goes in a file);
   - **an HTTP check** of `http://aviro-host:8081/api/health`.

   Each one's time until every required checklist item ticks is recorded as the *integration time to first data*.
3. **A deploy key.** The harness posts AVIRO's deployment events with it, as CI would.

The harness is only an API client of SENTINEL. It never reads SENTINEL's database.

## A run

1. **Baseline:**
   - AVIRO on `main`, with no faults, the agent running and the disk emptied;
   - the scenario's load (Locust shoppers);
   - 60 s to settle.
2. **Inject** the scenario's steps. The fault start is when the first step begins.
3. **Observe** for `observe_s`, then read every incident and telemetry gap SENTINEL opened since the fault start.
4. **Record fixtures** for the window, then **recover**, return to the baseline, and wait (at most 15 minutes) until nothing about the environment is firing.
5. **Close** that run's incidents through the API: true positives are resolved and closed, anything else is marked a false positive. So one run can't leak into the next.

A run that fails inside the harness (not inside SENTINEL) is recorded with its error, counted as **invalid** and not scored. The report shows it.

## Metrics

Defined in [`sentinel/evals/metrics.py`](../backend/src/sentinel/evals/metrics.py), and unit-tested on synthetic ground truth:

| Metric | Definition |
|---|---|
| Precision | True-positive incidents / all incidents opened about the environment during runs. An incident is a true positive if it was opened in the run's window and involves one of the scenario's expected entities |
| Recall | Runs expecting an incident that got a true positive **within the deadline** (default 5 min) / runs expecting one. Also shown per repeat (mean, min, max) |
| Time to detect | The first true positive's `detected_at` minus the fault start: median, p90, max. Detections after the deadline are timed too, and counted separately |
| False positives per hour | False positives during fault-free observation (S08 and the soak) / hours observed |
| Grouping accuracy | Runs with exactly one incident about the environment, and it a true positive / runs expecting one |
| Origin accuracy | S05, S12, S13, S14: the incident's primary entity is the expected origin |
| Gap classification accuracy | S15, S16: a telemetry gap with the expected classification and severity |

Incidents about anything else on the same SENTINEL (other integrations in a dev database) are counted separately and excluded.

## Running it

```bash
make stack-up                       # normal intervals: realistic detection times
export SENTINEL_EVAL_EMAIL=... SENTINEL_EVAL_PASSWORD=...   # a responder or admin
export SENTINEL_AGENT_PACKAGE_DIR=...   # optional: cached agent downloads (slow network)
make eval-detection REPEATS=3       # about 3.5 hours per repeat (measured)
make eval-soak RESUME=evals/results/<folder> HOURS=2
make eval-report RESUME=evals/results/<folder>
make eval-host-down
```

`RESUME=` continues an interrupted suite: runs already in `per_run.jsonl` are skipped. Results are written after every run.

## Known limits of this setup

- **One Podman VM.** The AVIRO host and SENTINEL share the VM's CPUs, so S14's CPU hog also slows SENTINEL. The disk, though, is separate.
- **S14 can't be detected within 5 minutes.** `HostHighCpu` needs a 5-minute average above 90%, held for 10 minutes. The run is watched for 20 minutes, so the report shows when it was detected; recall still counts the 5-minute deadline.
- **S15 and S16 wait for staleness.** A telemetry gap needs the integration to go stale first (about 5 minutes), so their deadline is 8 minutes.
- **We wrote both the faults and the detector.** A held-out set of scenarios, written later and not used for tuning, is the guard against overfitting (design §2).

## Changing targets

The PRD's targets are provisional. Any change after a baseline is recorded here, with the reason.
