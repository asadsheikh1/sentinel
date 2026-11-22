# Step 10 report — Rule packs, signals, deployments, and webhooks

**Status:** 🟡 built and tested locally; CI results pending.
**PR:** (link added after opening). Also [aviro-shop PR #3](https://github.com/asadsheikh1/aviro-shop/pull/3) (found while running S01; see below).
**Date:** 2026-11-22

## What was built

| Area | Files |
|---|---|
| Rule packs | `deploy/prometheus/rules/packs/{host,container,service,database,endpoint,integration}.yml`: 21 alerts and 9 recording rules, all generic by label |
| Rule tests | `deploy/prometheus/tests/packs/*_test.yml` (promtool), run by `make test-rules`. Plus `tests/unit/test_rule_packs.py`, which checks that every alert has a firing test, is labelled, keeps its entity's identity labels, and is loaded by Prometheus |
| Signals | `sentinel/detection/signals.py`: Alertmanager v4 → one signal per alert episode. Repeats update it, resolution closes it, and a late "firing" never re-opens it. Labels are redacted and the entity is linked |
| Telemetry gaps | `sentinel/detection/gaps.py`: the disambiguation table (agent problem / host or network down / unknown / source stopped), re-classified as probes change, resolved when data returns, re-opened (not duplicated) when an integration flaps |
| Event store | `sentinel/events/store.py`: `record_event` with a mandatory dedupe key. Probe states, integration-status changes, deployments and GitHub events all use it |
| Deployments | `sentinel/ingestion/deployments.py`: a `deployment` event plus a typed row. Idempotent (`Idempotency-Key`, or the body hash). `previous_commit_sha` filled in. Linked to the service entity |
| GitHub | `sentinel/ingestion/github.py`, `sentinel/security/webhooks.py`: HMAC-SHA256 over the raw body (constant time), de-duplication by delivery ID. `deployment_status` (later statuses update the deployment), `push` (first lines of commit messages, redacted), `workflow_run` (completed) |
| Alertmanager | Delivers to `/api/v1/ingest/alertmanager` with an `alerts:write` key that the API provisions to a volume only Alertmanager mounts (`ingestion/alertmanager_key.py`). Grouped per alert and entity, `group_wait` 10 s |
| API | `POST /api/v1/ingest/{alertmanager,deployments,github}`; `GET /api/v1/signals`, `GET /api/v1/deployments`. A new access rule, `hmac` (GitHub), is in `docs/rbac.md` |
| Database | Migration 0005: `signals`, `deployments`, event types `push` and `workflow_run` |
| Entity lookup | `integrations/entities.py: resolve_entity`, from labels: check → container → service (by `service.name` attribute) → host |
| Tests | `tests/unit/test_detection_unit.py` (gap table, signatures, payload validation), `tests/db/test_detection.py`, `tests/gateway/test_detection_e2e.py` (a live 5xx burst → signal), and two agent tests (`test_disk_filling_up_raises_a_host_signal` and `test_agent_stopped_becomes_a_telemetry_gap`) |
| Docs | [Detection guide](../guides/detection.md) |

## Acceptance criteria (roadmap)

| Criterion | Result |
|---|---|
| S01 → signal in ≤ 3 min | ✅ automated test: a synthetic 20% 5xx burst through the gateway → signal **105 s** after the fault. Real S01: **130 s** after the bad release went live: AVIRO under load from 20 synthetic shoppers, real `make deploy REF=scenario/S01-bad`. Critical `ServiceHighErrorRatio` signals for `orders` (100% failing) and `gateway` (10.5%), linked to their service entities. The deployment arrived through the webhook. With the fix deployed, the signals resolved in 110 s |
| Host disk-fill fault → host signal | ⏳ CI (the fresh Ubuntu VM): a fill file pushes `/` past 90% and a `HostFilesystemFull` signal must arrive, linked to the host. Not run locally, because filling the Podman VM's disk would grow its image on your Mac by about 40 GB. The rule itself is promtool-tested, fast filling included |
| Agent stopped → `telemetry_gap` | ✅ DB tests for every row of the table (S15 agent problem, S16 host down). ⏳ live in CI: stop the agent → a firing gap → start it → resolved |
| 30 min of normal load → no signals | ✅ second run: **no new signals** while AVIRO served 63,448 requests under load from 20 synthetic shoppers. The only change in the window was an old gap from the first run *resolving*. The first run failed: no false alerts, but 120 flapping `telemetry_gap` signals (bug 4) |

## Bugs found and fixed during this step

1. **`EndpointDown` could never fire.** `count by (…)(count_over_time(…))` counts series (always 1), not samples. It's `sum by` now. Caught by its promtool test.
2. **An alert naming a deleted integration crashed the whole webhook** with a foreign-key violation. Every alert in that delivery would have been lost, and Alertmanager would have retried forever. The integration is now linked only if it exists. Caught by the fixture.
3. **Deployments weren't linked to their service.** A deployment says `orders`; the inventory knows `aviro/orders@dev`. Services are now matched by their `service.name` attribute. Found in the S01 run, with a regression test.
4. **Integrations that send in bursts flapped between stale and healthy.** The first 30-minute normal-load run produced **120 `telemetry_gap` signals** (and no false alerts). They came from the prober and a check integration, whose data only arrives when a check runs. Three fixes:
   - a check counts as fresh for its own interval;
   - the prober sends a heartbeat every 15 s;
   - a gap that recurs within 10 minutes re-opens the same signal instead of creating a new one.
5. **AVIRO lost its SENTINEL connection** (aviro-shop PR #3):
   - `make load` re-created the services without their SENTINEL settings;
   - `make up-sentinel` could leave the old containers running;
   - `make deploy` deployed without the overlay.

   S01 couldn't be measured until this was fixed.
6. **The Step 9 live prober test left its check behind**, and the check alerted forever on a target that no longer existed. It now deletes it.
7. Smaller ones:
   - a fragile test that expected the only API key in the shared database to be its own;
   - the GitHub endpoint's "not configured" answer, which is now `401` like any other unauthenticated call.

### Checking that the tests can actually fail
I broke four rules on a copy of the rules folder, and promtool caught all four:
- the error-ratio threshold changed from 5% to 50%;
- `offset` removed from the watched-process check;
- the "at least 2 results" minimum removed from `EndpointDown`;
- unlimited containers no longer excluded from the memory-limit alert.

## Decisions worth reviewing

1. **Signals use `entity_id` and `service_name`, not `service_id`.** The database design's `services` table comes with the repository steps. Entities already exist and cover hosts, containers and endpoints too. Deployments do the same.
2. **Two event types added:** `push` and `workflow_run`. The designed `event_type` enum had no place for GitHub code and CI events.
3. **A `telemetry_gap` signal kind added** to the designed `signal_kind` enum, because the roadmap requires one.
4. **The Alertmanager key is provisioned by the API**, the same pattern as the prober token. It's written `0644` because Alertmanager runs as another user. Only the two containers mount the volume.
5. **`ServiceHighErrorRatio` uses a 1-minute window**, with recording rules added for it, plus a 1-minute `for`. That's what makes ≤ 3 minutes reachable. The minimum-traffic condition (0.1 req/s) keeps an occasional failed health check from alerting.
6. **The deployments and GitHub endpoints are on the API, not the ingest endpoint**, as the API design lists them. Caddy serves only OTLP and agent files.

## Known limitations

- **No host signal tested locally** (above). It depends on CI.
- **A gap's "covering" probe is matched by name:** the check's target host against host and container names the agent reported. A check of `www.example.com` on a host named `web-1` isn't related, so its gap stays "unknown". An explicit link (integration ↔ check) would fix that later.
- **The database pack is silent** until the PostgreSQL receiver is configured (a Should item).
- **No cloud view** in the gap table until the AWS connector.
- **GitHub deployments:** one event per deployment, whose status is updated in the `deployments` row. The event's payload keeps the first status it saw.

## How you verify

```bash
cd "/Users/asadsheikh/Coding/SENTINEL Autonomous AI Site Reliability Engineer"
git checkout step-10-detection
make test-rules                                # promtool: every rule pack
make stack-up-fast && make test-gateway        # includes the live 5xx → signal test (~4 min)
# S01 with the demo shop (aviro-shop PR #3):
cd ../aviro-shop && make up-sentinel TOKEN=sit_... DEPLOY_KEY=sk_live_... && make load USERS=20
make deploy REF=scenario/S01-bad              # a signal within ~2 minutes:
curl -s -b cookies.txt 'http://127.0.0.1:8000/api/v1/signals?status=firing'
make deploy REF=scenario/S01-fix
```

## Not in this step (by design)

- Log error grouping: the `error_group_*` signals (Step 11).
- Grouping signals into incidents (Step 12).
- The UI for signals and events (frontend steps).
