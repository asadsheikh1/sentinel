# Step 12 report — Entity-aware correlation, severity, lifecycle, timeline

**Status:** 🟡 built and tested locally; CI results pending.
**PR:** [PR #12](https://github.com/asadsheikh1/sentinel/pull/12). Also [aviro-shop PR #4](https://github.com/asadsheikh1/aviro-shop/pull/4) (deploy events; see bug 3).
**Date:** 2026-11-25

## What was built

| Area | Files |
|---|---|
| Correlator | `sentinel/detection/correlator.py`: every 15 s, each signal not yet in an incident joins an active incident within 15 minutes, for the first reason that holds: `same_entity`, `same_service`, `related_entity` (topology, either direction, ≤ 3 hops of `runs_on`/`uses`/`fronts`), `shared_trace`, `deploy_window`. Otherwise a firing critical/warning signal opens one. A pure core (`match`, `related`), and one transaction per pass under an advisory lock |
| Topology from traces (I12) | `deploy/otel/gateway.yaml`: a `servicegraph` connector counts who calls whom as spans pass. `integrations/entities.py: service_graph, store_dependencies` turn the last hour's edges in Prometheus into `uses` relations between services of the same environment (source `traces`), every 2 minutes |
| Severity (B8) | `sentinel/incidents/severity.py`: rules R1–R4. Error ratio, breadth (services involved), entity criticality. Raised automatically, never lowered; every change on the timeline with its rule |
| Lifecycle | `sentinel/incidents/lifecycle.py`: the state machine, with `false_positive` from anywhere. Mitigated automatically after every signal has been resolved for 5 minutes; a new firing signal re-opens it |
| Timeline (B9) | `incident_events`, append-only: a trigger rejects UPDATE, and the app role has no UPDATE/DELETE |
| Incident service | `sentinel/incidents/service.py`: open, attach, re-assess, transition, comment, nearest deployment (including one stamped up to 2 min after the start, or reported late), queue the investigation |
| Investigation | `sentinel/incidents/investigation.py`: one `investigate` job per incident on the `investigations` queue (de-duplicated); the handler is a stub until Step 17 |
| Alert reconciliation | `detection/signals.py: AlertReconciler`: reads Alertmanager's active alerts every 60 s. It resolves signals whose alert vanished, and takes in alerts that were never delivered (bugs 4, 5) |
| API | `GET /api/v1/incidents` (filters, cursor paging), `GET /{id}`, `PATCH /{id}` (status, severity, title; audited), `GET /{id}/timeline`, `POST /{id}/comments`. In `docs/rbac.md` |
| Database | Migration 0007: `incidents` (`INC-n`), `incident_entities` (origin/affected), `incident_signals` (with reason), `incident_events`. Relation source `traces` |
| Rule pack | `ServiceHighErrorRatio` gets a `value` annotation (the ratio), read by severity rule R2 |
| Docs | [Incidents guide](../guides/incidents.md); [detection guide](../guides/detection.md) updated |

## Acceptance criteria (roadmap)

| Criterion | Result |
|---|---|
| S01 → 1 incident with linked deployment | ✅ Real AVIRO under load from 20 synthetic shoppers, `make deploy REF=scenario/S01-bad`, with no restarts in between: **one incident** (INC-6). Details below the table |
| Disk fill on the AVIRO host plus resulting errors → 1 incident with both entities | ✅ DB test with the real topology shape: host ← container ← service. A host `HostFilesystemFull` signal and a 5xx signal of the service on it make one incident, with the host as origin and the service affected. An unrelated host's disk alert stays separate. ⏳ Live in CI, on the fresh Ubuntu VM: a real `HostFilesystemFull` from the agent, plus an app on that host logging `ENOSPC` errors through the real ingest path → one incident with both entities. The app's errors are sent by the test, because the disk is filled to 93%, not 100%. Not run locally: filling the Podman VM's disk would grow its image on your Mac by about 40 GB |
| S08 (3× traffic, no faults) → 0 incidents | ✅ 60 synthetic shoppers instead of 20 for 15 minutes, watched for 20: traffic peaked at **48.5 req/s against a 15.6 req/s baseline (3.1×)**, with 42,714 gateway requests. **0 incidents, 0 signals** |

**S01 in detail (INC-6):**
- **Detection:** the incident opened **2 min 41 s** after `make deploy` started.
- **Severity:** SEV1, because 100% of orders fail (R2).
- **Deployment:** S01-bad linked (`orders scenario/S01-bad`).
- **Gateway:** its 5xx joined through the `uses` relation learnt from traces (`related_entity`).
- **Recovery:** after the fix, the alerts resolved in 90 s. The incident became `mitigated` 5 minutes later, with exactly one status change.
- **The KeyError's error group didn't raise a signal this time.** It was already known from earlier runs, so it wasn't new, and its 5× spike threshold was measured against an hour that already held those runs. That's correct behaviour for a recurring bug.

## Test results

| Suite | Result |
|---|---|
| `make test-all` (unit + DB, coverage gate 85%) | 701 passed, coverage 93.8% |
| New unit tests (`test_incidents_unit.py`: state machine, severity table, topology, matching) | 70 passed |
| New DB tests (`test_incidents.py`: correlation tables, lifecycle, timeline, API, deployment linking, reconciliation, host attribution) | 14 passed |
| `make test-rules` (promtool) | all passed, incl. the new `value` annotation |
| `make test-gateway` (live stack) | 23 passed; the 5xx burst now also opens an incident. The delivery test passed 3 times in a row (it failed 1 in 3 before bug 10's fix); 0 Jaeger restarts |
| CI, first run | 3 failures, which became bugs 9–12 below |
| CI, after the fixes | pending |

**Checking that the tests can actually fail.** I broke eight guarantees in the source:
- topology in one direction only;
- no correlation window;
- `info` signals opening incidents;
- automatic severity able to go down;
- no rollout grace for deployments;
- mitigation without the quiet period;
- a closed incident able to re-open;
- the reconciler resolving on the first miss.

At first, seven were caught. The severity one wasn't, because no test proved that automatic re-assessment never *lowers* severity. That test is added, and now all eight are caught.

## Bugs found and fixed during this step

All were found in the live runs, and every fix has a regression test.

1. **An earlier info signal missed the incident it belonged to.** Signals are taken oldest first. An `info` signal that predates the incident it belongs to, opened later in the same pass, was left out until the next pass. Fix: leftovers get a second look in the same pass.
2. **Deployments weren't linked when stamped at the end of the rollout.** S01's errors began at 20:54:51, during the rollout; the deployment was stamped at 20:54:54, after it. Fixes:
   - a deployment up to 2 minutes *after* the start counts;
   - for 30 minutes, the correlator keeps looking for a deployment reported late (a CI job).
3. **Redeploying a commit wasn't recorded** (aviro-shop). AVIRO's deploy script used `deploy-<commit>-<service>` as its idempotency key, from my Step 10 change, so deploying S01-bad again was taken as a retry. Fixes in AVIRO's script:
   - the key is unique per run;
   - `deployed_at` is stamped when the rollout starts.
4. **Alert signals fired forever after an Alertmanager restart.** Alertmanager keeps active alerts in memory, so a restart mid-alert means no "resolved" is ever sent. That also blocked incidents from mitigating. When fixed, it resolved 12 stuck signals, 10 of them from earlier days.
5. **New alert episodes were never delivered after an Alertmanager restart.** Its notification log survives restarts, and took S01's second run as a repeat of the first, held back for an hour. Fix: the reconciler also takes in active alerts SENTINEL lacks.
6. **The OpenTelemetry SDK's own errors became error groups.** When SENTINEL restarted, AVIRO logged "Failed to export … batch due to timeout": 10 new groups, attached to the S01 incident. These are about telemetry delivery, not the app, so the `opentelemetry.*` loggers are ignored.
7. **Spike signals flapped, and so did their incident.** A rate hovering at 5× resolved and re-fired its spike, and the incident went mitigated/open three times. Fixes:
   - spikes end below *half* their thresholds;
   - an incident mitigates only after 5 quiet minutes.
8. **Gateway e2e tests left integrations behind:** 298 of them. Each one goes stale and, since this step, opens a telemetry-gap incident on the stack the tests ran against. The test fixture now disables what it created.

Four more were found by the first CI run:

9. **Reading the topology from the trace store killed it.** Jaeger's dependencies API loads every trace of the window into memory. Asked for an hour every 5 s (the fast test stack), Jaeger was **OOM-killed 61 times in 15 minutes** at its 512 MB limit. The edges now come from the gateway's `servicegraph` connector, through Prometheus. Live, the same five AVIRO relations appeared, with 0 Jaeger restarts.
10. **The reconciler raced the webhook.** It took an alert in seconds after it fired, before Alertmanager's 10 s `group_wait`. An alert that resolved inside that window was then never notified at all, so its signal stayed firing until the reconciler's own 5-minute check (CI's delivery test, reproduced locally 1 time in 3). Alerts are now taken in only after 2 minutes active.
11. **Host alerts were attributed to the agent, not the host** (a Step 10 bug). Host metrics carry the reporting agent's `job` label, and the resolver's service fallback took that as the alert's service. So in CI's S13 run, the disk alert and the app's errors became two incidents: the agent's service and the app are siblings on the host, not related. The resolver now follows the alert's `entity_type` label. The CI test now checks the entity type, not just that there is one.
12. **A migration could fail on a DNS hiccup.** In CI, `sentinel db upgrade` started before Podman's DNS knew `postgres` ("Temporary failure in name resolution"), the same flake as in Step 9. It now waits up to 60 s for the database to accept connections, and retries only connection failures.

## Decisions worth reviewing

1. **`primary_entity_id` and `incident_entities` instead of `primary_service_id` and `incident_services`.** This follows the same entity-based design as Steps 10–11; the generalisation was already noted in the database design.
2. **`uses` relations from traces, via a new `servicegraph` connector in the gateway** (a change to the approved gateway configuration). This is I12's "relations", and without it S01's gateway errors would be a second incident. Querying the trace store instead isn't safe (bug 9). Traces name services by `service.name`, so an edge links same-named services within its environment.
3. **Attach reasons and event types added:**
   - reasons `first_signal`, `same_entity`, `related_entity` beyond the designed `same_service`, `shared_trace`, `deploy_window`;
   - events `opened` and `investigation_queued`.
4. **Automatic status changes:** `mitigated` after 5 quiet minutes, and back to `open` when a new signal fires. People resolve and close.
5. **What opens an incident:** firing `critical` or `warning` signals. `info` signals and already-resolved ones only join existing incidents.
6. **Two services sharing a host are not related;** only dependencies are (`runs_on`/`uses`/`fronts`, either direction).
7. **Investigation jobs are queued but nothing runs them yet** (Step 17).
8. **The Alertmanager reconciler** reads Alertmanager's API (read-only, inside the stack). It's a backstop for the webhook, not a replacement.

## Known limitations

- **The topology is only as good as discovery.** A service calling something without traces (a database, a third-party API) has no `uses` edge.
- **`fronts` isn't discovered yet** (it needs load-balancer or ingress data); only `runs_on` and `uses` are.
- **The deploy-window reason relies on the commit:** a release is "the same commit within 10 minutes".
- **Entity criticality is never set yet** (it will be in the inventory UI, Step 14), so R2/R3/R4's criticality cases only apply once someone sets it.

## How you verify

```bash
cd "/Users/asadsheikh/Coding/SENTINEL Autonomous AI Site Reliability Engineer"
git checkout step-12-incidents
make test-all                                  # unit + DB, incl. the correlation tables
make stack-up && cd ../aviro-shop && make up-sentinel TOKEN=sit_... DEPLOY_KEY=sk_live_... && make load USERS=20
make deploy REF=scenario/S01-bad              # an incident within ~3 minutes:
curl -s -b cookies.txt http://127.0.0.1:8000/api/v1/incidents
make deploy REF=scenario/S01-fix              # mitigated ~5 minutes after its alerts resolve
```

## Not in this step (by design)

- The investigator that runs the queued jobs (Step 17).
- The incident UI (Step 13).
