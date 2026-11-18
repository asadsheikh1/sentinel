# Step 6 report — Integration model, ingest tokens, OTLP ingestion gateway

**Status:** ✅ done. All acceptance criteria met. CI passes in both repos: 6 of 6 checks on SENTINEL and 2 of 2 on AVIRO.
**PRs:** [SENTINEL PR #6](https://github.com/asadsheikh1/sentinel/pull/6) and [AVIRO PR #2](https://github.com/asadsheikh1/aviro-shop/pull/2) (not merged; merging is your decision)
**Date:** 2026-11-18

## What was built

| Area | Details |
|---|---|
| Front door | Caddy on port 4319 accepts only OTLP `POST /v1/{metrics,logs,traces}`, removes client-sent identity headers, and removes the token before forwarding |
| Token check | `/internal/ingest-auth` validates the token, enforces its per-minute limit and records last-seen per signal. Only Caddy can call it (shared secret) |
| Gateway Collector | OpenTelemetry Collector 0.165: identity overwrite, normalisation, secret redaction, metric label guard, export to Prometheus, Loki and Jaeger |
| Integration registry | Create, list, view, update, disable. Tokens (`sit_…`) are shown once, stored only as a keyed hash, and can be rotated or revoked |
| Health | `pending`, `healthy`, `degraded`, `stale`, `revoked`, `disabled`, recalculated every 30 s, with status changes in the audit log |
| Entity discovery | Services, hosts and containers, with `runs_on` relations, discovered from telemetry, including logs-only and traces-only sources |
| AVIRO | Onboarded through the gateway with its own token (`make up-sentinel TOKEN=…`) |
| Database | Migration `0003`: `integrations`, `ingest_tokens`, `integration_signal_status`, `entities`, `entity_relations` |
| Docs | [Ingestion gateway guide](../guides/ingestion-gateway.md), regenerated `database.md` and `rbac.md` |

## Acceptance criteria (roadmap)

| Criterion | Result |
|---|---|
| AVIRO telemetry arrives only through the gateway | ✅ AVIRO's containers can reach the ingest endpoint. Prometheus, Loki, Jaeger, the Collector, the API and SENTINEL's Postgres are all unreachable from them |
| Queryable with the correct `sentinel.integration.id` | ✅ metrics, logs and traces from AVIRO all carry its integration ID and kind |
| Normalised attributes | ✅ tested end to end: `host` → `host.name`, `deployment.environment` → `deployment.environment.name`, `err` → severity 17, service name from the container |
| Shows `healthy` | ✅ with metrics, logs and traces all arriving |
| `stale` within 5 minutes of stopping | ✅ **294 seconds**, measured with production timings |

### Roadmap test list

| Test | Result |
|---|---|
| No token or revoked token → 401 at Caddy | ✅ |
| Client-supplied `sentinel.integration.id` is overwritten | ✅ on metrics, logs and traces, and via spoofed headers |
| Normalisation rules | ✅ against the real Collector |
| Rate limit returns 429 | ✅ with `Retry-After` |
| Last-seen updates | ✅ |
| Entity discovery | ✅ unit, database and end to end |

### Test totals (local)

| Suite | Result |
|---|---|
| Backend unit + database + integration (`make test-all`) | 140 passed, coverage 93.1% |
| Gateway end to end (`make test-gateway`) | 13 passed |
| Telemetry stores (`make test-obs`) | 5 passed |

### Checking that the tests can actually fail
I removed identity overwrite, normalisation and redaction from the gateway. Five end-to-end tests failed, as they should, and all passed again once the config was restored.

That check also exposed one weak test: "service name from the container" still passed, because Loki fills in that name itself. It now checks Prometheus instead.

## Problems found and fixed

1. **The stack hung in CI for an hour.** `api` depended on the one-shot `migrate` container. Once `migrate` exits, GitHub's Podman 4.9 tries to restart it and fails; your Mac's Podman 6.2 doesn't. `api` now depends only on Postgres, and `make` waits for the migration and stops with its logs if it failed. The CI job also has a time limit now.
2. **Loki always reported `unhealthy` (from Step 5).** Its health check used `wget`, which the image doesn't include. The check is removed; readiness comes from Loki's `/ready` endpoint.
3. **Collector component names changed in version 0.165** (`otlp_http`, `delta_to_cumulative`, and resource rules inside each signal). Found by validating the config against the real binary.

## Decisions worth reviewing

1. **ORM models live in `sentinel/db/models/integrations.py`,** not `sentinel/integrations/models.py` as the roadmap listed. Migrations must see every table, and the database layer can't import from a higher layer.
2. **Health and discovery jobs run inside the API process for now,** because last-seen data is held in that process's memory. They can move when the job system arrives (Step 17).
3. **`stale` uses a 270-second threshold** rather than 300, so that with the 30-second check interval it's reported within 5 minutes.
4. **AVIRO joins a dedicated `sentinel-ingest` network** containing only Caddy. That's what enforces "only through the gateway".

## Known limitations

- **Series limit reports but doesn't drop.** An integration over 5,000 metric series is marked `degraded`, but the excess series are still stored. The design says excess should also be dropped. Unbounded labels (user IDs, full URLs) are already removed.
- **The AVIRO onboarding check was run on this Mac, not in CI,** because CI for one private repo can't check out the other. The gateway itself is fully tested in CI.
- **No TLS yet.** Port 4319 is localhost-only. TLS arrives with the AWS deployment (Step 33).
- **Tail sampling isn't included.** Every trace is stored for now.

## How you verify

```bash
cd "/Users/asadsheikh/Coding/SENTINEL Autonomous AI Site Reliability Engineer"
git checkout step-06-ingestion-gateway
make stack-up-fast && make test-gateway
make stack-up
# Then follow docs/guides/ingestion-gateway.md to register a system and send it telemetry.
make stack-down
```

## Not in this step (by design)

Reading stored telemetry from SENTINEL and the full redaction engine (Step 7). The agent for Linux hosts (Step 8). The integrations screens (Step 14).
