# Detection: signals, deployments, and change events

SENTINEL turns telemetry and outside events into two kinds of record:

- **Signals:** something may be wrong, for example a service failing requests,
  a disk filling up, an endpoint down, or a source gone quiet. Signals are
  grouped into incidents in Step 12.
- **Events:** something changed, for example a deployment, a code push, a CI
  run, a probe changing state, or an integration's health changing. Events are
  what an investigation looks at to answer "what changed?".

```mermaid
flowchart LR
  T[Telemetry in Prometheus] --> R[Rule packs] --> AM[Alertmanager] -->|webhook, API key| S[(signals)]
  G[Integration goes stale] --> GAP[gap classifier] --> S
  L[Error logs in Loki] --> EG[error poller: fingerprint, count] --> S
  CI[CI / deploy script] -->|POST /ingest/deployments| E[(events + deployments)]
  GH[GitHub] -->|signed webhook| E
  P[Prober state changes] --> E
```

## Rule packs

The rules live in `deploy/prometheus/rules/packs/`, one file per entity type.
They're written against labels only, so a new host, container or service is
covered as soon as its data arrives.

| Pack | Alerts |
|---|---|
| host | `HostHighCpu` (> 90%, 10 min), `HostMemoryLow` (< 10% available), `HostFilesystemFull` (> 90%), `HostFilesystemFillingUp` (full within 4 h at the last 30 min's rate), `HostInodesHigh`, `HostWatchedProcessAbsent`, `HostNetworkErrors` |
| container | `ContainerRestarted`, `ContainerMemoryNearLimit` (only containers that have a limit), `ContainerCpuThrottled` |
| service | `ServiceHighErrorRatio` (> 5% 5xx with real traffic, 1-minute window), `ServiceLatencyHigh` (p95 > 1 s **and** over twice its 1-hour baseline), `ServiceThroughputCollapse` (< 20% of the last hour) |
| database | `DatabaseConnectionsHigh`, `DatabaseDeadlocks`, `DatabaseReplicationLag` (for the PostgreSQL receiver, when it's configured) |
| endpoint | `EndpointDown` (every result in 2 minutes failed, at least 2), `EndpointSlow`, `EndpointCertExpiringSoon` (< 14 days) |
| integration | `AgentExportFailing`, `AgentQueueNearlyFull` (from the agent's own metrics) |

Every alert carries `severity`, `pack`, `entity_type` and the entity's
identifying labels (`host_name`, `container_name`, `service_name`,
`sentinel_check_id`, …), plus a one-line `summary`.

`make test-rules` runs promtool on all of them. Every alert has a test that it
fires, and the packs' tests include cases that must stay quiet:
- a healthy second host;
- a single failed check;
- a service that is always slow (above 1 s, but that's its normal);
- a 3× traffic spike;
- an unlimited container;
- the gateway's own Collector, as opposed to an agent.

## Signals

`GET /api/v1/signals?status=firing&kind=&service=&entity=&since=` (any signed-in role)

| Kind | From |
|---|---|
| `prom_alert` | A rule-pack alert, via Alertmanager. One signal per *episode*: the same alert firing again later is a new signal. Repeats and retries update it. A resolved signal is never re-opened by a late delivery |
| `telemetry_gap` | An integration went `stale` (below) |
| `error_group_new` | A log error SENTINEL hasn't seen before in that service (or host unit). Resolves after 10 quiet minutes |
| `error_group_spike` | A known error at 5× its usual rate (below) |

Signals are linked to the inventory entity they're about. An alert's
`entity_type` label decides first: a host alert belongs to the host, even
though host metrics carry the reporting agent's `job`. Otherwise the lookup
checks, in order: the health check, the container, the service, then the host.

### Telemetry gaps: agent problem, or host down?

A source going quiet doesn't say *why*. SENTINEL uses what else it can see,
mainly external checks of the same machine:

| Source | Endpoint checks of that machine | Classification | Severity |
|---|---|---|---|
| agent | all pass | agent problem | info |
| agent | any fails | host or network down | critical |
| agent | none | host or agent (unknown) | warning |
| app, webhook, … | — | source stopped sending | warning |

The classification is updated as probes change. The signal resolves when data
flows again. A check "covers" a machine when its target host matches a host or
container name that the agent reported.

## Log error groups

Every 30 s SENTINEL reads the error logs that arrived since last time and
groups them. The same error with different IDs in it is **one group**,
however many requests hit it.

`GET /api/v1/error-groups?service=&entity=&since=&seen_since=` and
`GET /api/v1/error-groups/{id}` (any signed-in role). The detail has the
latest sample (message, stack trace, trace ID), the last ~70 minutes of counts
per minute, and the group's signals.

**What counts as an error:** a record with severity ERROR or worse, or one
that carries an exception. A traceback or a JSON logger's `"level": "error"`
counts too, even when the record says INFO, as container output read from the journal may.
A line from a file or the journal goes by its own level when it has one, so
an app that logs INFO to stderr isn't flagged.

**The fingerprint:** a hash of three things.
- **The exception type**, e.g. `KeyError`.
- **The message, normalised:**
  - UUIDs, numbers, e-mail and IP addresses, timestamps, URLs, hex values and
    long quoted values become placeholders;
  - short quoted names stay, because `KeyError: 'sku'` and
    `KeyError: 'price'` are different bugs.
- **The top in-app frame:** the innermost line of the stack trace in the app's
  own code, as `file:function` without the line number. A redeploy that moves
  the line keeps the same group.
  - "In-app" means not the standard library or a third-party package. An
    app's code is often installed in `site-packages` too, so its package is
    taken from where the error was logged.
  - Frameworks that log on an app's behalf (uvicorn, Django, …) never count
    as the app.

A plain ERROR line without a stack trace uses where it was logged instead of a
frame.

**Not grouped:** the OpenTelemetry SDK's own messages (loggers `opentelemetry.*`),
such as "Failed to export logs batch due to timeout". They arrive as the app's
errors, but they're about telemetry delivery, which a telemetry gap already
covers.

**Groups belong to a scope:**
- **A service:** `service:{namespace}/{name}@{environment}`.
- **Without a service:** the host's container, systemd unit or syslog
  identifier, then the host itself.

**Spikes:** a group at least 15 minutes old raises `error_group_spike` when
both of these hold for the last 5 minutes:
- at least 20 occurrences;
- at least 5 times its average 5 minutes over the hour before.

It resolves once the rate falls below half of both thresholds (so a rate hovering at 5× doesn't flap). The thresholds are settings
(`SENTINEL_ERROR_SPIKE_*`).

**Reliability:** Loki is read up to 20 s behind real time, so lines that arrive
late are still counted. One pass is one database transaction: the groups,
their signals and how far it has read are committed together. A pass that
fails (Loki down, a restart) commits nothing, and the next one reads the same
lines again. Every line is counted exactly once.

## Deployments

```bash
curl -X POST https://sentinel.example.com/api/v1/ingest/deployments \
  -H "X-Sentinel-Key: $SENTINEL_DEPLOY_KEY" -H "Idempotency-Key: deploy-$GIT_SHA-orders" \
  -H 'Content-Type: application/json' \
  -d '{"service": "orders", "version": "v1.4.0", "commit_sha": "'$GIT_SHA'",
       "image_ref": "registry/orders:v1.4.0", "environment": "prod", "deployed_by": "ci"}'
```

- **Key:** create an API key with the `deployments:write` scope (`POST /api/v1/api-keys`).
- **Duplicates:** `Idempotency-Key` makes retries safe. Without it, an identical body is still recorded only once.
- **Response:** `201` for a new deployment, `200` for a repeat.
- **What's stored:** each deployment is a `deployment` event plus a row in `GET /api/v1/deployments?service=&since=`, linked to the service entity. `previous_commit_sha` is filled from the last successful deployment.
- **AVIRO:** run `make up-sentinel TOKEN=… DEPLOY_KEY=…` once. After that, `make deploy REF=…` reports every rollout.

## GitHub

The webhook is `POST /api/v1/ingest/github`, with the secret in
`SENTINEL_GITHUB_WEBHOOK_SECRET`:

- **Content type:** `application/json`.
- **Events:** *Pushes*, *Deployment statuses* and *Workflow runs*.

| Event | Becomes |
|---|---|
| `deployment_status` | A deployment (`source: github`). Later statuses update it: `success` → `succeeded`, `failure`/`error` → `failed` |
| `push` | A `push` event: the ref, before and after commits, and up to 20 commits with the first line of each message, with secrets redacted |
| `workflow_run` (`completed`) | A `workflow_run` event. A failure is `warning` |
| `ping` | Nothing stored (`pong`) |

How it's protected:
- **Signature:** every delivery's `X-Hub-Signature-256` is checked against the raw body. A wrong or missing signature gets `401`.
- **Duplicates:** deliveries are de-duplicated by `X-GitHub-Delivery`.
- **Size:** bodies over 5 MB are refused.
- **No secret configured:** the endpoint answers `401` with "not configured".

## Alertmanager

Alertmanager sends to `/api/v1/ingest/alertmanager` with an API key whose only
scope is `alerts:write`. SENTINEL creates this key itself on startup and writes
it to a volume that only Alertmanager mounts. A valid key is kept, and an
invalid one is replaced.

Grouping is per alert and entity, with `group_wait` set to 10 s, so a new
alert reaches SENTINEL quickly.

**Kept in step with Alertmanager.** The webhook is the fast path, but it can
miss things. Every 60 s, SENTINEL reads Alertmanager's active alerts (a
read-only API) and reconciles both ways. Both cases below were seen live in
Step 12:
- **A resolved alert it never heard about.** Alertmanager keeps active alerts
  only in memory. Restarted while an alert fired, it forgets the alert and
  never sends "resolved". A firing signal whose alert is missing from
  Alertmanager on two checks in a row is resolved, with the reason
  "no longer active in Alertmanager".
- **A firing alert that was never sent.** Alertmanager's notification log
  survives a restart. It can take a new episode of an alert it notified before
  as a repeat, and hold it back for the 1-hour repeat interval. An alert that
  has been active for 2 minutes with no firing signal is taken in through the
  same idempotent path as the webhook. Not sooner: that would race the webhook,
  and an alert that resolves within `group_wait` is never notified at all.

If Alertmanager can't be reached, nothing changes.

## How fast

| Scenario | Measured |
|---|---|
| S01 (a deploy makes `POST /api/orders` fail) | Signal **130 s** after the bad release went live (191 s from the start of `make deploy`, which includes a 60 s image build). Resolved 110 s after the fix went live |
| Synthetic 20% 5xx burst (`tests/gateway/test_detection_e2e.py`) | Signal 105 s after the fault (locally), then resolved |
| Normal load, 30 minutes (AVIRO, 20 shoppers, 63,448 requests) | No signals |
| S01 error group (Step 11) | One new group, `KeyError: gift_message` at `aviro/orders/app.py:create_order`. Its signal was recorded **34 s** after the first error reached Loki |
| Normal load, 12 minutes (Step 11) | No new error groups |
