# Step 16 report: evaluation harness, scenarios S01–S16 and measured detection

**Status:** ✅ built and tested. The detection suite was run for real: 16 scenarios × 3 repeats, plus a 2-hour soak. The numbers below are measured, and most of them are well below the PRD's targets (that is what this step is for).
**PR:** [PR #16](https://github.com/asadsheikh1/sentinel/pull/16).
**Date:** 2026-11-27

## What was built

| Area | Files |
|---|---|
| Scenario schema | `backend/src/sentinel/evals/scenario.py`:<br>• Steps: deploy, fault, clear faults, container, data, disk fill, CPU hog, agent, host, load.<br>• `detection`: expect an incident or not, deadline, observation window, expected entities, origin, expected telemetry gap.<br>• `ground_truth`, for the root-cause steps from Step 18. |
| Scenarios | `evals/scenarios/S01…S16/scenario.yaml`, one fault each, taken from AVIRO's real fault commits and flags |
| The AVIRO host | `evals/host/`:<br>• a disposable Ubuntu 24.04 systemd machine on SENTINEL's ingest network;<br>• AVIRO inside it in its own Podman, deployed with AVIRO's own `scripts/deploy.sh`;<br>• its own 6 GiB ext4 disk for `/var/lib/containers`, so S13 can fill it without touching SENTINEL;<br>• an edge proxy on port 8081. |
| Orchestrator | `evals/runner.py`. It:<br>• sets up three integrations through SENTINEL's API (app, container host with the agent installed by the real installer, HTTP check) and a deploy key;<br>• runs baseline, inject, observe, record fixtures, recover, wait until quiet, then closes the run's incidents.<br>The harness is only an API client: `evals/client.py`. |
| Metrics | `evals/metrics.py`: precision, recall within the deadline, time to detect, false positives per hour, grouping, origin and gap classification |
| Results | `evals/results.py` writes `per_run.jsonl` after every run, plus `metrics.json` and `report.md`; `RESUME=` continues an interrupted suite |
| CLI and Make | `sentinel eval run / soak / report`; `make eval-detection`, `eval-soak`, `eval-report`, `eval-host-down` |
| Docs | [evals/README.md](../../evals/README.md) |
| AVIRO | [aviro-shop PR #5](https://github.com/asadsheikh1/aviro-shop/pull/5) (merged): after buying, half the shoppers check their order's status, so S11's endpoint gets traffic |

## Acceptance criteria (roadmap)

| Criterion | Result |
|---|---|
| A committed results folder with real detection metrics | ✅ [`evals/results/2026-11-26_00a87697_detection/`](../../evals/results/2026-11-26_00a87697_detection/report.md): 48 runs and the 2-hour soak, 0 invalid |
| Testing: metric functions tested on synthetic ground truth | ✅ 44 unit tests in `tests/unit/test_evals_unit.py` |
| Scenario fixtures recorded | ⚠️ 48 fixtures (760 KB gzip-compressed, 17 MB uncompressed): logs and metrics for every run, but **traces for only 13 runs** (Jaeger was crashing, see finding 7) |

## Measured results

The full report, with every run, is [report.md](../../evals/results/2026-11-26_00a87697_detection/report.md).

| Metric | Measured | PRD target |
|---|---|---|
| Recall within 5 min | **55%** (23 of 42 runs). Per repeat: 43%, 57%, 64% | ≥ 90% |
| Detected after the deadline | 4 runs (345–399 s) | |
| Precision | **46%** (33 true, 38 false, including the soak's 2) | |
| Time to detect | median **140 s**, p90 275 s | |
| False positives per hour, fault-free | **1.2** (3 in 2.5 h: S08 and the 2-hour soak) | ≤ 1 |
| Grouping: exactly one incident per fault | **19%** | |
| Origin (S05, S12, S13, S14) | **0 of 12** | |
| Gap classification (S15, S16) | **5 of 6** | |
| Agent stopped, but no service incident (S15) | 3 of 3 | |
| Integration time to first data | app 112 s, host 136 s, check 61 s | |

**By scenario:**
- **Always detected (3 of 3):** S06 memory leak, S10, S13 disk full, S16 host down.
- **Usually detected (2 of 3):** S01, S05, S07, S09, S11.
- **Never detected within the deadline:** S02, S03, S04 (subtle deploy and config faults) and S12. S04 was detected once, after 374 s.
- **S14** (CPU hog): detected once in 3 runs. As expected, `HostHighCpu` needs a 5-minute average held for 10 minutes, which can't fit in the 5-minute deadline.

## Findings: SENTINEL problems this step measured (not fixed here)

1. **Container log noise.** Each container's stderr becomes its own error group, and so its own SEV3 incident.
   - 23 of the 38 false positives have a container as their primary entity.
   - 12 of those are Postgres `LOG:` lines (checkpoints), which aren't errors. They include both of the soak's false positives.
   - This is also why grouping is 19%: a fault in orders is opened as one incident for the service and another for `aviro_orders_1`.
2. **The origin is never the primary entity** (0 of 12).
   - S05: the incident is about orders and the gateway; payments, the slow dependency, is in it in only 1 of 3 runs.
   - S13: the host is in the incident, but the primary entity is the Postgres container.
3. **Telemetry-gap incidents have no entity** (6 false positives). In S15, an agent stopping opens a SEV3 incident with no entity, even though the gap itself is correctly classified `agent_problem` / info.
4. **Host alerts unrelated to the fault** (6 false positives about `aviro-host`).
   - 5 are `HostFilesystemFillingUp`: S02, S03 and S05 in repeat 2, and S12 in repeats 1 and 2. None of those scenarios fills the disk.
   - 1 is `HostHighCpu` (S15 run 3).
   - Why the disk alert's prediction fires there isn't known yet.
5. **S12 (a harmless deploy during a payments slowdown) was never detected.** The slowdown that S05 catches 2 times in 3 wasn't caught once while the deploy happened alongside it, and each run's only incident was about the host or inventory.
6. **One gap misclassified** (S15 run 1: `host_or_agent` / warning instead of `agent_problem` / info).
7. **Jaeger crashes once its store grows.**
   - Badger reached 17.6 GB within a day of eval load, with 7-day retention.
   - At its 512 MB memory limit, it was killed for running out of memory 888 times, from S03 run 1 onwards.
   - Detection doesn't read traces, so the numbers above aren't affected. The trace fixtures are missing.
   - **Your decision (option A):** fix Jaeger as its own approved change, and re-record the trace fixtures before Step 18.
8. **SENTINEL commits a request's transaction after it has sent the response.**
   - FastAPI's default for a `yield` dependency (`api/deps.py`, `get_db_session`) runs its exit code, and so the commit, after the response is sent.
   - So a request sent right after a write can miss it. Measured: a request right after login got 401, and the same request 0.5 s later got 200.
   - This stopped the first soak attempt. The web UI is exposed to it too.
   - **Your decision:** fix it as a separate change after Step 16. Until then, the harness waits after logging in until its session works.

## Bugs found and fixed during this step

1. **AVIRO host bring-up** (rootless Podman on the Mac):
   - Loop devices aren't allowed rootless, so the disk is made in the Podman VM.
   - Nested netavark calls `systemd-run --user`, so a wrapper drops `--user`.
   - CNI had no DNS, so netavark and aardvark-dns are installed.
   - A `podman exec -i` held stdin and hung.
   - A cloned bundle needed `main` checked out first.
2. **Shoppers ran inside the AVIRO host,** so the agent collected Locust's output as errors. They now run outside it, on SENTINEL's ingest network.
3. **Leftovers between runs:**
   - still-active incidents of other things absorbed new signals, so they are closed before each fault;
   - integrations from earlier trials still alerted, so they are disabled at setup.
4. **The quiet wait could never end** on a recurring error group. It now waits only on alerts and telemetry gaps (at most 15 minutes).
5. **Reopened telemetry gaps weren't counted.** Gaps are now read by activity window.
6. **Fixture recording failed as a whole** when one store failed. It is now per store.
7. **The soak couldn't start:** observation was capped at 1 hour. The cap is now 12 hours, with tests.
8. **The report printed p90 and max unrounded.**
9. **Fixtures were too big to commit** (up to 2.3 MB each; the repo's hook allows 500 KB). `FixtureTelemetryAdapter` now reads and writes gzip for a `.gz` path, with reproducible bytes, and the fixtures are compressed. `per_run.jsonl` was rewritten only to change each fixture path to `.json.gz`.

## Decisions worth reviewing

1. **A false positive is any incident about the environment that doesn't involve an expected entity.** That includes S15's gap incident, which SENTINEL classifies correctly but still opens as an incident.
2. **A telemetry-gap incident counts as a true positive in S16** (host down), though it has no entity (finding 3). S15 expects no incident at all, so there it is a false positive.
3. **Origin means the primary entity,** not just "is in the incident". That's strict, and it's what a responder sees first.
4. **The dev database was backed up and reset** for a clean run (your approval). Restoring it is offered below.

## Known limitations

- **One Podman VM.** The AVIRO host and SENTINEL share its CPUs, so S14's CPU hog also slows SENTINEL.
- **Three repeats** give wide ranges (recall 43–64% per repeat). Differences of a run or two between scenarios aren't significant.
- **We wrote both the faults and the detector.** A held-out scenario set (design §2) is still to come.
- **Traces are missing from 35 fixtures** (finding 7).
- **The soak's fixture is empty.** Fixtures are queried for the scenario's expected entities, and the soak has none.
- **The suite ran with commit `00a8769`.** The soak ran with the harness fixes that came after it (items 7 and 8 above, and the wait after login). The fixtures were compressed afterwards (item 9).

## Test results

| Suite | Result |
|---|---|
| `make lint` (ruff, format, mypy strict, import contracts, frontend) | Clean |
| `make test-all` (unit + db + integration) | 802 passed. 44 of them are new evaluation tests (metrics on synthetic ground truth, the scenario schema, every scenario file, results and the report, the soak scenario) and 2 are new gzip fixture tests |
| Coverage | **86.3%, down from 94.0%** (the minimum is 85%). The scoring code (`metrics.py`, `scenario.py`, `results.py`) is at 98–100%. The runner, AVIRO host and API client (550 statements) have no unit tests: they drive real infrastructure, and the 49 real runs exercised them instead. |
| The suite itself | 48 runs, then the soak: 0 harness errors |
| CI on PR #16 | All 11 checks green on the first run |

## Afterwards

- Your dev database can be restored from `~/sentinel-backups/sentinel-dev-db-2026-11-26.dump`, and the old Jaeger data from `~/sentinel-backups/jaegerdata-2026-11-26.tar` in the Podman VM. I haven't restored them; tell me if you want that.

## How you verify

```bash
cd "/Users/asadsheikh/Coding/SENTINEL Autonomous AI Site Reliability Engineer"
git checkout step-16-eval-harness
cat evals/results/2026-11-26_00a87697_detection/report.md
make eval-report RESUME=evals/results/2026-11-26_00a87697_detection   # recomputes metrics.json and report.md
```

A short real run takes about 25 minutes (`make stack-up`, then a responder's credentials):

```bash
export SENTINEL_EVAL_EMAIL=... SENTINEL_EVAL_PASSWORD=...
cd backend && uv run sentinel eval run --scenarios S10 S16 --repeats 1
```

The full suite (`make eval-detection REPEATS=3`) takes about 10.5 hours, and the soak 2 hours more.
