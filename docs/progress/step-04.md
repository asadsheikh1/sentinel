# Step 4 report — AVIRO demo shop with fault injection

**Status:** ✅ done. All acceptance criteria met in CI (rootless Podman on Linux). See "Local Mac issue" below for what couldn't be completed on your Mac.
**Repo / PR:** [`aviro-shop`](https://github.com/asadsheikh1/aviro-shop), branch `step-04-aviro` → [PR #1](https://github.com/asadsheikh1/aviro-shop/pull/1) (not merged; merging is your decision)
**Date:** 2026-11-16

## What was built (in `aviro-shop`)

| Part | Details |
|---|---|
| Services | `gateway` (the only public port, 127.0.0.1:8080), `inventory`, `payments`, `orders` (FastAPI), `worker` (Celery on Valkey), PostgreSQL 17. One image, rootless Podman compose |
| Checkout flow | Reserve stock (atomic) → charge card → save order → write receipt to disk → queue fulfilment → worker marks it `fulfilled`. Declined cards release stock |
| Telemetry | Standard OpenTelemetry **zero-code instrumentation** (`opentelemetry-instrument`). Endpoint, headers and exporters come from standard `OTEL_*` variables (off by default). JSON logs include `trace_id` and `span_id`. `make up-otel` adds a debug collector |
| Runtime faults | Admin API with a token check and auto-expiry (default 5 min): `latency`, `error_rate`, `dependency_timeout` (database or a service), `memory_leak`, `cpu_hog`, `disk_fill`, `process_crash` |
| Realistic limits | `inventory` has a 300 MB memory limit (a leak ends in a real OOM kill and restart). `orders` writes receipts to a 200 MB tmpfs (`disk_fill` causes real "No space left on device" errors) |
| Code-fault scenarios | Real commits and tags on `scenario/*` branches: S01, S02, S03, S04, S09, S11 (`-bad` and `-fix`) and S12 (`-deploy`). Commit messages read like normal work, and the bad commits pass the unit tests, like a real faulty release |
| Data scenario | S10: a latent bug triggered by inserting one catalogue row, with no deploy |
| Tools | Locust load generator (`make load`), fault CLI (`make fault`), `scripts/deploy.sh <ref>` (builds that ref and emits a deployment event to SENTINEL once Step 10 exists) |
| Tests | 35 unit tests. 15 live smoke tests (every fault plus telemetry). `verify_scenarios.py` (deploys every scenario tag and checks its symptom) |
| CI | Lint, types and unit tests. Then the **full stack on rootless Podman:** smoke tests, telemetry check, and all 13 scenario deploys |

## Acceptance criteria (roadmap)

| Criterion | Result |
|---|---|
| `podman compose up` gives healthy services | ✅ on your Mac (before the VM issue) and in CI |
| Every fault reproduces | ✅ **15/15 smoke tests in CI** (14/14 fault tests on your Mac; the telemetry test needs the collector, see below) |
| Unit tests per service | ✅ 35 passed |
| OTel exported to a configurable endpoint | ✅ CI: traces, metrics and logs from all 5 services reached the collector, tagged `service.namespace=aviro` |
| Code faults as real commits and tags | ✅ 13 tags, all verified live in CI |

### Live fault results (CI)
latency ✅ · error_rate → 502 ✅ · dependency timeout (service) → 504 in ~3 s ✅ · dependency timeout (db) → Postgres cancels the query → 500 ✅ · memory leak → RSS +80 MB ✅ · memory leak → **OOMKilled=true** and restart ✅ · CPU hog ✅ · disk fill → checkout 500 (ENOSPC), then recovery ✅ · crash → restart ✅ · broker down (S07) ✅ · S10 data bug ✅ · telemetry from all 5 services ✅

### Scenario results (CI, `verify_scenarios.py`)
```
PASS  scenario/S01-bad       checkout without gift message → 500
PASS  scenario/S01-fix       checkout → 201
PASS  scenario/S02-bad       median /api/products 14.2 ms
PASS  scenario/S02-fix       median 4.0 ms; bad version was 3.6x slower
PASS  scenario/S03-bad       25/60 concurrent checkouts failed; PoolTimeout logged
PASS  scenario/S03-fix       0/60 concurrent checkouts failed
PASS  scenario/S04-bad       order not fulfilled; worker task error logged
PASS  scenario/S04-fix       order fulfilled
PASS  scenario/S09-bad       checkout → 500; injected text present in logs
PASS  scenario/S09-fix       checkout → 201
PASS  scenario/S11-bad       GET order → 500
PASS  scenario/S11-fix       GET order → 200
PASS  scenario/S12-deploy    harmless deploy, checkout → 201
```

## Bugs and problems found along the way

1. **`orders` crashed on startup** because `EmailStr` needs the `pydantic[email]` extra. Fixed. A test now imports every service module so this class of error is caught.
2. **`disk_fill` didn't free space when cleared.** CI found this on Linux. The fill thread held the file open, and an open file's space is only released when its last handle closes. Fixed: the file is now opened only for each write.
3. **First S01 and S09 bad commits failed their own unit tests.** A real faulty release passes CI, so the bad commits now update the test fixture to include the new field, while normal clients still don't send it.
4. **S09's injected log line was placed after the crash,** so it was never logged. It's moved before the crash, and the check verifies the text appears in the logs.
5. **The S03 check used fast reads,** which never exhausted the pool. It now uses concurrent checkouts, where 25 of 60 fail with `PoolTimeout` on the bad config and none on the fix.
6. **Podman tmpfs rejects `uid=`.** Changed to `mode=1777`.

## ⚠️ Local Mac issue: the Podman VM clock nearly stops

On your Mac, the Podman VM (provider **libkrun**) ran at roughly **one fifth of real time** while waiting on the network. Measured: the guest clock advanced 20 s while the Mac's advanced 95 s. Inside the VM, chrony reports "Not synchronised".

**Effects:**
- Image downloads and builds stall, often for 15 minutes or more.
- Container timestamps drift by 20 to 55 minutes.

`caffeinate -dims` (keeping the Mac awake) didn't help, so it isn't caused by sleep.

**What still worked locally:**
- The full build.
- All 14 fault smoke tests.
- The order flow.

**What couldn't finish locally:** pulling the collector image for the telemetry test, and the scenario deploys. Both pass in CI on Linux instead.

**My recommendation:** recreate the Podman VM with macOS's standard **applehv** provider instead of libkrun. Commands are in the reply to you. **This deletes the VM's images and local test databases** (no real data), so it's your call. Steps 5+ run more containers locally (Prometheus, Loki, Jaeger), so fixing this matters.

## How you verify

```bash
cd /Users/asadsheikh/Coding/aviro-shop
git checkout step-04-aviro
make install && make lint && make test
make up                                  # http://127.0.0.1:8080
curl 127.0.0.1:8080/api/health
make smoke                               # every fault reproduces
make fault ARGS="set payments latency ms=8000"
curl -s -o /dev/null -w "%{http_code} in %{time_total}s\n" -X POST 127.0.0.1:8080/api/orders \
  -H 'content-type: application/json' \
  -d '{"customer_email":"me@example.com","items":[{"sku":"KB-101","qty":1}],"card_token":"tok_visa_test"}'
make fault ARGS="clear payments"
make deploy REF=scenario/S01-bad         # then: uv run python scripts/verify_scenarios.py S01
make down
```

## Not in this step (by design)

Sending AVIRO's telemetry to SENTINEL (SENTINEL Step 6, the ingestion gateway). Deployment events reaching SENTINEL (Step 10). Host-level scenarios S13–S16 (Steps 8–9, the agent).
