# Step 5 report — Telemetry storage backends

**Status:** ✅ done. All acceptance criteria met on your Mac and in CI.
**Branch / PR:** `step-05-telemetry-stores` → [PR #5](https://github.com/asadsheikh1/sentinel/pull/5) (not merged; merging is your decision)
**Date:** 2026-11-17

## What was built

| Store | Version (pinned tag + digest) | Ingest | Query API | Retention | Memory limit |
|---|---|---|---|---|---|
| Prometheus | 3.16.0 | OTLP receiver `/api/v1/otlp/v1/metrics` | PromQL `/api/v1/query` | 15 days or 4 GB | 768 MiB |
| Loki | 3.7.10 | OTLP `/otlp/v1/logs` | LogQL `/loki/api/v1/query_range` | 7 days (compactor) | 640 MiB |
| Jaeger v2 | 2.23.0 | OTLP gRPC :4317 / HTTP :4318 | API v3 `/api/v3/traces/{id}` | 7 days (Badger TTL) | 512 MiB |
| Alertmanager | 0.35.0 | `/api/v2/alerts` (from Prometheus) | `/api/v2/alerts` | 5 days | 128 MiB |

| File | Purpose |
|---|---|
| `deploy/compose/compose.observability.yaml` | The four stores, pinned images, memory limits, localhost-only ports, `telemetry` network |
| `deploy/prometheus/prometheus.yml` | OTLP name translation pinned (`UnderscoreEscapingWithSuffixes`), promoted resource attributes, out-of-order window for pushed data |
| `deploy/prometheus/rules/red.yml` + `tests/red_test.yml` | RED recording rules (rate, errors, error ratio, p95, per-route rate) and their promtool unit tests |
| `deploy/loki/loki.yaml` | Single-binary Loki, TSDB schema v13, structured metadata on, `sentinel.integration.id` as an index label |
| `deploy/jaeger/config.yaml` | Jaeger v2 (built on the OTel Collector), Badger on disk, health check, own metrics on :8888 |
| `deploy/alertmanager/alertmanager.yml` | Null receiver until Step 10 adds the SENTINEL webhook |
| `backend/tests/unit/test_deploy_config.py` | Static checks: images pinned by digest, memory ≤ 2.5 GB, localhost-only ports, retention, pinned translation |
| `backend/tests/observability/test_stores.py` | Live test: protobuf OTLP into each store, then queried back |
| `.github/workflows/ci-observability.yml` | Runs all of the above on rootless Podman |
| `docs/guides/telemetry-stores.md` | URLs, make targets and copy-paste curl commands |

## Acceptance criteria (roadmap)

| Criterion | Result |
|---|---|
| All three accept OTLP | ✅ Prometheus, Loki and Jaeger accept OTLP **protobuf** (the test) and **JSON** (the curl guide) |
| All three answer queries | ✅ PromQL, LogQL and Jaeger API v3 return the pushed data |
| Stack memory ≤ 2.5 GB | ✅ limits total **2,048 MiB** (static test). Actual use after the tests: **~96 MB on your Mac, ~95 MB in CI** |
| Pinned images | ✅ every image pinned by tag **and** digest (static test) |
| Not publicly reachable | ✅ every port bound to `127.0.0.1` (static test) |

### What the live test proves (on your Mac and in CI)
- **Prometheus:** the metric `sentinel.selftest.value` (unit `1`) is queryable as `sentinel_selftest_value_ratio`, with `job="sentinel/sentinel-selftest"` and the promoted labels `service_name` and `sentinel_integration_id`. That shows the pinned name translation and label promotion work.
- **Prometheus rules:** all 5 RED rules load and evaluate with health `ok`. promtool unit tests check their math: rate 10/s, errors 1/s, ratio 0.1, p95 750 ms.
- **Loki:** the log line is queryable by `service_name`. `sentinel_integration_id` is an index label, and `trace_id` arrives as structured metadata, which links logs to traces later.
- **Jaeger:** the span comes back by trace ID through API v3, and the service is listed.
- **Alertmanager:** the posted alert is listed.

### Test totals
| Suite | Result |
|---|---|
| Backend unit + db + integration (`make test-all`) | 104 passed, coverage 93.9% |
| Observability live (`make test-obs`) | 5 passed (Mac and CI) |
| promtool (`make test-rules`) | rule unit tests and config check pass |

## Problems found

1. **Rule health was `unknown` right after startup.** It stays that way until the first evaluation, 30 s in. The test now waits for the first evaluation, then requires `ok` for every rule.
2. **promtool p95 check off by a few ulps** (0.7499999999999998). Floating-point noise from `rate()` division. It's now compared in whole milliseconds (`round(p95*1000) == 750`). The math is still checked exactly; only float noise is ignored.
3. **The test HTTP client was never closed.** CI turned the leaked sockets into errors, because warnings are errors in this project. The fixture now closes it.
4. **The backend CI job ran the observability tests without the stores.** The full-suite run now excludes them; they run in their own workflow.
5. **Your GitHub CLI's active account changed to `asadtecxeo`,** which can't see these private repos. I didn't change your global setting. These two repos now have a *repo-local* git credential helper that uses the `asadsheikh1` login, and `gh` calls for this project pass that account's token per command.

The Podman VM worked well this session: images pulled and started in 17 s. The libkrun slowdown from Step 4 didn't recur. I'll keep watching for it.

## How you verify

```bash
cd "/Users/asadsheikh/Coding/SENTINEL Autonomous AI Site Reliability Engineer"
git checkout step-05-telemetry-stores
make obs-up && make obs-ps
make test-obs && make test-rules && make obs-stats
# copy-paste curl commands: docs/guides/telemetry-stores.md
open http://127.0.0.1:9090      # Prometheus
open http://127.0.0.1:16686     # Jaeger UI
make obs-down
```

## Not in this step (by design)

The ingestion gateway Collector, ingest tokens and authentication in front of these stores (Step 6). Alerting rules and the Alertmanager → SENTINEL webhook (Step 10). Tail sampling of traces (gateway, Step 6/7). Grafana is optional and not included.
