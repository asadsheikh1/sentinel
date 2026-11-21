# Step 7 report — Telemetry query adapters and redaction

**Status:** done locally. CI: CI_RESULT
**PR:** [PR #7](https://github.com/asadsheikh1/sentinel/pull/7) (not merged; merging is your decision)
**Date:** 2026-11-21

## What was built

| Area | Files |
|---|---|
| Shared types | `telemetry/base.py`: `Selector` (service, namespace, environment, host, integration), `TimeRange` (max 7 days), `Limits`, errors, byte-capped HTTP reads |
| Metrics | `telemetry/prometheus.py`: 6 fixed templates (request rate, error rate, error ratio, p95, p99, named series), automatic step so a series stays under 600 points |
| Logs | `telemetry/loki.py`: selector + optional text filter + minimum severity. Every line is sanitised and redacted, and only known labels are kept |
| Traces | `telemetry/jaeger.py`: one trace by ID, or recent traces (optionally errors only). Spans flattened, attributes cleaned |
| Record and replay | `telemetry/fixtures.py`: `FixtureTelemetryAdapter` saves real responses and replays them offline, ignoring timestamps when matching |
| Redaction | `security/redaction.py`: known secret formats, auth headers, URL credentials, `key=value` secrets, emails, Luhn-checked card numbers, and a high-entropy fallback. Trace IDs, span IDs, UUIDs and SHAs are kept. ANSI and control characters are stripped |
| Debug CLI | `sentinel telemetry metrics / logs / traces / trace`, with `--json`, `--record` and `--replay` |
| Fixtures | `tests/fixtures/telemetry/aviro.json`: real responses recorded from AVIRO traffic through the gateway, including a payments fault. `tests/fixtures/redaction/corpus.yaml`: 31 fake secrets and 19 benign lines |
| Docs | [Reading telemetry safely](../guides/telemetry-queries.md) |

## Acceptance criteria (roadmap)

| Criterion | Result |
|---|---|
| Adapters pass | ✅ 31 tests on recorded real responses, plus query-building, injection, cap and error tests |
| Redaction corpus passes | ✅ **31 of 31 seeded secrets removed (100%)**. All 19 benign lines unchanged |

### Roadmap test list

| Test | Result |
|---|---|
| Recorded-response unit tests | ✅ replay real Prometheus, Loki and Jaeger responses offline |
| Integration against live backends | ✅ data sent through the gateway is read back by integration. One integration's filter never returns another's data |
| Redaction corpus | ✅ 100% |
| Property tests | ✅ 4 Hypothesis properties over 1,100 generated inputs: idempotent, no control characters, a secret is removed wherever it appears, length cap respected |

### Test totals (local)

| Suite | Result |
|---|---|
| Backend unit + database + integration (`make test-all`) | 240 passed, coverage 93.6% |
| Live gateway + adapters (`make test-gateway`) | 15 passed |
| Telemetry stores (`make test-obs`) | 5 passed |

### Checking that the tests can actually fail
I turned off the high-entropy detector. Five redaction tests failed: the two corpus secrets that only that detector catches, the 100% score, the allowlist test, and one property test. With the code restored, all passed.

## Bugs found and fixed during this step

1. **Error-trace search found nothing.** It fetched the newest traces, then filtered for errors, so errors older than the newest few were missed. It now asks Jaeger to filter (`error=true`), which I confirmed returns exactly the 6 error traces out of 57.
2. **Recording broke on compressed responses.** The recorder stored the decompressed body but kept the `Content-Encoding: gzip` header, so the client tried to decompress it twice.
3. **The CLI crashed on an unrecorded response** in replay mode. It now prints a clean error.
4. **The redaction engine took the key along with a secret** (`random=…` became one redacted token), because `=` was allowed inside a candidate. `=` is now accepted only as trailing base64 padding.

## Decisions worth reviewing

1. **Layering.** `sentinel.telemetry` sits between `integrations` and `auth` in the layering contract: it depends only on `security` (redaction) and `config`.
2. **Gitleaks exception for test fixtures.** `.gitleaks.toml` exempts exactly two fixture files, which contain fake secrets on purpose. I checked that a fake secret in any other file is still caught.
3. **`clean_value` drops sensitive keys outright.** A value under a key like `authorization`, `password` or `api_key` is replaced entirely rather than pattern-matched.

## Known limitations

- **No redaction engine is complete.** An unusual secret format with low entropy can get through. The gateway's redaction and this one are two layers; the corpus is where new cases should be added.
- **Emails are always redacted.** That includes system addresses that might be useful as evidence.
- **The structlog redaction processor isn't added yet.** The security doc mentions one; SENTINEL's own logs don't carry telemetry yet.

## How you verify

```bash
cd "/Users/asadsheikh/Coding/SENTINEL Autonomous AI Site Reliability Engineer"
git checkout step-07-telemetry-adapters
cd backend && uv run pytest -q tests/unit/test_redaction.py tests/unit/test_telemetry_adapters.py && cd ..
# offline, from the recorded responses:
cd backend && uv run sentinel telemetry logs --service fixture-secrets --replay tests/fixtures/telemetry/aviro.json && cd ..
# live (start AVIRO as in the Step 6 guide first):
make stack-up
cd backend && uv run sentinel telemetry metrics error_ratio --namespace aviro
uv run sentinel telemetry traces --service orders --errors
```

## Not in this step (by design)

Turning query results into stored evidence with IDs (Step 17, collectors). Error grouping from logs (Step 11). Prompt-injection flagging of evidence (with the investigation, Step 18).
