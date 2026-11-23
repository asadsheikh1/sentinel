# Step 11 report — Log error grouping and fingerprinting

**Status:** 🟡 built and tested locally; CI results pending.
**PR:** (link added after opening).
**Date:** 2026-11-23

## What was built

| Area | Files |
|---|---|
| Traceback parser | `sentinel/detection/traceback.py`: CPython tracebacks, including chained (the raised exception is the last one), `SyntaxError` frames, `[Previous line repeated]`, notes, multi-line messages, a traceback that starts inside a log line, and exception groups (the group is what was raised). Total: any text in, a result or `None` out |
| Fingerprint | `sentinel/detection/fingerprint.py`: hash of (exception type, normalised message, top in-app frame). Message: IDs, numbers, e-mails, IPs, timestamps, URLs, hex values and long quoted values become placeholders; short quoted names stay. Frame: `file:function`, no line number, with the app's own packages recognised inside `site-packages` |
| Error groups | `sentinel/detection/error_groups.py`: a line → an occurrence (scope, level, fingerprint), folded into `error_groups`. `error_group_new` (resolves after 10 quiet minutes) and `error_group_spike` signals, linked to the inventory entity |
| Poller | `sentinel/detection/poller.py`: reads Loki forward from a watermark, 20 s behind real time. Pages without losing or double-counting lines that share a timestamp. One pass is one transaction, and an advisory lock means one poller. Runs every 30 s in the API's background jobs |
| Loki | `telemetry/loki.py: LokiClient.entries` (forward, nanosecond timestamps, error labels kept, redacted, a stack trace cut from the front so its end survives). `deploy/loki/loki.yaml`: gRPC message limits raised (bug 1) |
| API | `GET /api/v1/error-groups`, `GET /api/v1/error-groups/{id}` (any signed-in role; in `docs/rbac.md`) |
| Database | Migration 0006: `error_groups` (unique per scope and fingerprint), `poll_watermarks` |
| Tests | `tests/unit/test_error_grouping_unit.py` (59), `tests/db/test_error_groups.py` (8), `tests/fake_loki.py`, a corpus of 13 real tracebacks in `tests/fixtures/tracebacks/`, and a live test in `tests/gateway/test_detection_e2e.py` |
| Docs | [Detection guide](../guides/detection.md), "Log error groups" |

## Acceptance criteria (roadmap)

| Criterion | Result |
|---|---|
| S01 → exactly one new error group with the correct top frame | ✅ Real AVIRO under load from 20 synthetic shoppers, real `make deploy REF=scenario/S01-bad`. **One** new group: `orders`, `KeyError: gift_message`, top frame **`aviro/orders/app.py:create_order`** (the line the bad commit changed). It's linked to the `orders` service entity. Its `error_group_new` signal was recorded **34 s** after the first error reached Loki. 75 failed requests at that point, all in the one group. See "the first S01 run" below |
| Hypothesis property tests | ✅ The parser never raises, and round-trips a generated traceback. Normalising is idempotent. The same error with different IDs gives one fingerprint. Run with six different seeds |
| Traceback corpus | ✅ 13 tracebacks printed by a real CPython 3.12, plus the real S01 trace from Loki. Every one parses to the right type, message, chain count and innermost frame |
| Poller restart safety with fixtures | ✅ Against a fake Loki:<br>• a pass whose Loki read fails commits nothing, and the retry counts every line once;<br>• a crash *after* counting commits nothing;<br>• a pass repeated over the same window counts nothing twice;<br>• 101 lines read in 10-line pages, 2 pages per pass, across several passes, with lines sharing a nanosecond, give exactly 101;<br>• a second poller skips while the first holds the lock |

### The first S01 run: two groups, and why
The first time this SENTINEL saw a deploy, two groups were created:
- **The S01 `KeyError`.**
- **`gateway: upstream failed`** (8 lines): AVIRO's deploy script restarts every service, and for about a second the gateway couldn't reach `inventory`. That's a real error, and it's correctly its own group. It isn't S01.

For the measured run that gateway group was already known, as it is on any system that has deployed before. I deleted only the old `KeyError` group, so S01 could be new again. That deploy didn't trigger the restart error at all: the gateway group stayed at 8. Either way, S01 itself is exactly one group.

### Also measured
- **Normal load:** 12 minutes of AVIRO under load (20 shoppers) after the fix: **no new error groups**, and no error lines at all.
- **Quiet resolution:** both "new error" signals resolved by themselves 10 minutes after their group's last line (21:30:33 → 21:41:02).
- **Live gateway test:** OTLP exception logs through the real ingest gateway, Loki and poller → one group with the right top frame and one signal. Passed.

## Test results

| Suite | Result |
|---|---|
| `make test-all` (unit + DB, coverage gate 85%) | 613 passed, coverage 93.8% |
| New unit tests (`test_error_grouping_unit.py`) | 59 passed |
| New DB tests (`test_error_groups.py`) | 8 passed |
| `make test-gateway` (live stack, rootless Podman) | 23 passed, incl. the new error-group test |
| CI | pending |

**A local environment problem, not a SENTINEL bug:** one gateway run failed two tests that have nothing to do with this step. The Podman VM's clock had fallen 2 h 15 min behind the Mac's, so test data stamped with the Mac's time looked 2 hours in the future, and Prometheus rejected it.
- **Fix:** I set the VM clock from the Mac, gave the telemetry stores fresh volumes (Postgres kept), and the suite passed.
- **The S01 numbers above are unaffected:** all their timestamps came from the VM.

## Bugs found and fixed during this step

1. **Loki couldn't return a page of error lines.** 2,000 lines, each with its full stack trace, is about 4.8 MB. Loki's querier and frontend talk over gRPC inside the one process, with a 4 MB message limit. The querier failed, and the query hung until its 60 s timeout. The poller logged `TelemetryTimeoutError` and read nothing. Fixes:
   - Loki's gRPC limits raised to 32 MB;
   - pages cut to 500 lines, with up to 20 pages per query per pass.

   Found in the S01 run. The same query now takes 0.5 s.
2. **Property tests found two normaliser gaps:**
   - a quoted name with a number in it (`'order_42'`) normalised differently the second time;
   - unusual but valid e-mail addresses (`/@A.VE`) weren't replaced.

   Both are fixed.

### Checking that the tests can actually fail
I broke six things in the source, and a test failed each time:
- paging that counts a shared timestamp twice;
- a watermark that skips what a truncated query didn't read;
- every `site-packages` frame treated as the app;
- line numbers in the fingerprint;
- quiet resolution inverted;
- stderr INFO lines treated as errors.

## Decisions worth reviewing

1. **Groups belong to a scope, not a `service_id`.** The scope is the service (`namespace/name@environment`), or, for a source without one, the host's container, unit or the host. Signals and groups also carry `entity_id`, as in Step 10. The designed `services` table comes later.
2. **`minute_counts` (the last ~70 minutes per minute) instead of `last_window_count`.** A spike needs a baseline, and a single window count can't give one.
3. **`poll_watermarks` has `last_ns` as well as `last_ts`.** Paging is exact to the nanosecond, which Postgres timestamps can't store.
4. **Spike thresholds:** at least 20 in 5 minutes, at least 5× the hour's average, and the group at least 15 minutes old (a younger group is "new", not a spike). These are settings.
5. **The poller runs in the API process,** like the other background jobs so far, not in a separate `sentinel-scheduler` container. The advisory lock already makes a second process harmless when that split happens.
6. **What's an error:** severity ERROR or worse, or an exception attached. A traceback, or a JSON `"level": "error"`, counts in an INFO line. A line from a file or the journal goes by its own level first. Through the journal, a container's output gets its level from the stream it was written to, not from the line, so uvicorn's INFO lines on stderr must not count as errors. A unit test covers this. The live agent path wasn't measured in this step

## Known limitations

- **Python tracebacks only.** Other languages' errors are still grouped, by type, message and where they were logged, but without a top frame from the stack.
- **A traceback split across journal lines isn't joined.** Container output through journald arrives one line per record, so a multi-line traceback printed to stdout becomes several records. A file source with `multiline_start` (Step 7), or OTLP logs, keep it whole.
- **Late lines:** a line that reaches Loki more than 20 s after its timestamp, after its window was read, isn't counted. Agent retries after a long outage can do that.
- **Spike baseline:** at most the last hour, and only from when SENTINEL started counting.
- **More than 500 lines at exactly the same nanosecond** would lose the excess (logged). Not seen in practice.

## How you verify

```bash
cd "/Users/asadsheikh/Coding/SENTINEL Autonomous AI Site Reliability Engineer"
git checkout step-11-error-groups
make test-all                                  # unit + DB tests, incl. restart safety
make stack-up && cd ../aviro-shop && make up-sentinel TOKEN=sit_... DEPLOY_KEY=sk_live_... && make load USERS=20
make deploy REF=scenario/S01-bad              # within about a minute:
curl -s -b cookies.txt http://127.0.0.1:8000/api/v1/error-groups
make deploy REF=scenario/S01-fix
```

## Not in this step (by design)

- Grouping signals into incidents (Step 12).
- Mapping frames to repository files at the deployed commit (D3, with the repository steps).
- The UI for error groups (frontend steps).
