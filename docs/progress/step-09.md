# Step 9 report — External health checks (prober)

**Status:** ✅ done. All acceptance criteria met locally and in CI (10 of 10 checks pass; the live prober tests ran in CI's ingestion gateway job: 19 of 19).
**PR:** [PR #9](https://github.com/asadsheikh1/sentinel/pull/9) (not merged; merging is your decision)
**Date:** 2026-11-22

## What was built

| Area | Files |
|---|---|
| SSRF guard | `sentinel/prober/ssrf_guard.py`. Refuses bad host syntax before DNS (credentials, internal names, single-label names, ambiguous numeric IPs). Resolves, then requires **every** answer to be public; IPv6 forms that embed IPv4 (mapped, NAT64, 6to4, Teredo) are judged by the embedded address. Pins the connection to the vetted IP |
| Checks | `sentinel/prober/checks.py`. HTTP(S) (GET only, no proxy, no cookies, manual redirects with each hop vetted again, body read capped at 64 KiB, status / body / JSON-field assertions), TCP, and TLS (verification and expiry, optional `min_days_valid`). The whole check is bounded by its timeout |
| Scheduler | `sentinel/prober/scheduler.py`. A new check first runs at a random offset within 5 s. After that it runs every interval ±5%, with no drift and no catch-up bursts |
| Results | `sentinel/prober/otlp.py`. `probe_success`, `probe_duration_seconds`, `probe_http_status_code` and `tls_cert_expiry_seconds` as OTLP through the gateway. One resource per check. The failure reason goes to a WARN log record, never into a label |
| Prober process | `sentinel/prober/run.py`, `sentinel prober run`. Fetches its checks and sends results only through the ingest endpoint, with its own token. Re-reads a rotated token, and keeps a bounded backlog while SENTINEL is unreachable |
| Server side | `sentinel/checks/service.py`: CRUD, the same guard at creation (DNS included), the 50-check cap, minimum interval, audit log, and an endpoint entity per target. `checks/prober_identity.py`: the prober integration, with its token written to a volume only the prober reads. `checks/state.py`: up / down (two failures in a row) / unknown, decided from Prometheus, with a `probe_state` event on every change |
| API | `/api/v1/checks` (GET, POST, GET/PATCH/DELETE by ID, `integrations:manage`). `/api/v1/prober/checks` (new **prober-token** access rule). `/api/v1/events?type=&entity=&since=` (viewers) |
| Database | Migration 0004: `health_checks`, `events`, and the integration kind `prober` |
| Deployment | `prober` service, read-only with no capabilities, on its own `sentinel-probe` network (egress + Caddy). A volume carries the prober's token. Caddy routes the prober's work list. Prometheus promotes the check labels |
| Layering | Import contracts: `sentinel.prober` may use only config and logging (no db, auth, API, …) |
| Tests | `test_ssrf_guard.py` (143), `test_prober_checks.py` (16, real local servers incl. TLS via trustme), `test_prober_runtime.py` (14), `tests/db/test_checks.py` (22), `tests/gateway/test_prober_e2e.py` (3, live) |
| Docs | [External health checks](../guides/health-checks.md), `docs/rbac.md` and `docs/database.md` regenerated |

## Acceptance criteria (roadmap)

| Criterion | Result |
|---|---|
| The SSRF suite passes 100% | ✅ **143 of 143**. Every case on the roadmap and security-doc lists is covered (details below) |
| An AVIRO endpoint going down is visible in metrics within 2 intervals | ✅ Real AVIRO gateway, 30 s interval: the first failed result was in Prometheus **26 s** after `podman stop` (limit 60 s). The state was `down` at 57 s and `up` again 29 s after restart, each change stored as an event. Live test target, 10 s interval: first failure after 5–6 s |

### SSRF suite (roadmap list → result)

| Case | Result |
|---|---|
| `127.0.0.1`, `[::1]`, `localhost` | ✅ refused |
| `169.254.169.254` (and AWS IMDS over IPv6 `fd00:ec2::254`, Alibaba `100.100.100.200`, Oracle `192.0.0.192`) | ✅ refused |
| `10.x`, `172.16/12`, `192.168/16`, CGNAT `100.64/10`, reserved and multicast ranges | ✅ refused |
| Decimal and octal IP forms (`2130706433`, `0177.0.0.1`, `0x7f.1`, `127.1`, …) | ✅ refused, with the reason "ambiguous numeric address" |
| IPv6 embedding: `::ffff:127.0.0.1`, NAT64 `64:ff9b::7f00:1`, 6to4, Teredo | ✅ refused. Python's own `is_global` calls the NAT64 form global, so this needed its own rule |
| DNS rebinding (test resolver answers public, then private) | ✅ resolved once; the request goes to the vetted IP with the original `Host` header |
| A name with one private answer among public ones | ✅ refused |
| Redirect to private (`169.254.169.254`, `localhost`, decimal IP, `file://`, a name that resolves privately) | ✅ refused at the hop. More than 5 redirects stop |
| URL credentials, `file://`, `gopher://`, CR/LF injection, bad ports | ✅ refused |
| SENTINEL's own container names (`api`, `postgres`) | ✅ refused at creation. The prober's network can't reach them either (live test) |
| Property tests (Hypothesis) | ✅ any non-global IPv4/IPv6 is refused; mapped and NAT64 forms inherit the verdict |

### Roadmap test list

| Test | Result |
|---|---|
| SSRF suite | ✅ 143 passed (above) |
| Behaviour against local test servers: slow, 500, bad cert | ✅ slow → `timeout` within the check timeout; 500 → fails (unless expected); bad cert (unknown CA), wrong host name and expired cert → `tls: …`; also redirects, JSON and body assertions, a 1 MiB body capped, refused and closed ports, TLS expiry and `min_days_valid` |
| Live: endpoint down → metrics, state, events | ✅ |

### Test totals (local)

| Suite | Result |
|---|---|
| Backend unit + database + integration (`make test-all`) | 492 passed, coverage 93.4% |
| Live gateway + prober (`make test-gateway`) | 19 passed |
| CI (PR #9) | 10 of 10 checks pass, including the 3 live prober tests on rootless Podman |

### Checking that the tests can actually fail
I broke the guard five ways:

| Mutation | Caught? |
|---|---|
| NAT64 decoding removed | ✅ caught |
| A private DNS answer tolerated when there are others | ✅ caught |
| Redirect hops no longer vetted | ✅ caught |
| Numeric-form rule removed | ❌ not caught at first |
| Multicast check removed | ❌ not caught |

The last two weren't caught because both rules have a second layer: those inputs are still refused, as a single-label name, a name ending in a number, or an address in the explicit block list. I added tests that pin the numeric rule's reason, and with it removed 6 tests now fail. The multicast rule stays a redundant layer, since every multicast range is also in the explicit list.

## Bugs found and fixed during this step

1. **`probe_success` would have been named `probe_success_ratio`.** A gauge with unit `1` gets a `_ratio` suffix in Prometheus. The unit is now empty.
2. **Errors in nested assertions lost their location.** A bad `json_field.path` gave a message without the `assertions.` prefix. Fixed.
3. **A stopped container was reported as "blocked".** When its DNS name disappears, the error read `blocked: … does not resolve`. It now reads `dns: …`, and creation says "Target does not resolve". Found in the AVIRO demo.
4. **Exports could lose results.** A failed send dropped the batch. Results are now kept, up to 2,000.

## Decisions worth reviewing

1. **The `events` table is created now, not in Step 10.** Step 9 asks for probe-state `SentinelEvent`s, but the roadmap lists `events` under Step 10's DB changes. I created it exactly as designed in `docs/2-architecture/4-database.md`, and only `probe_state` events are written. `GET /api/v1/events` (from the API design) also comes now, so the events can be seen. Step 10 adds the other event types. This changes the build order, not the design.
2. **How the prober gets its identity and work.** The prober has no database or API access. The API provisions one integration of kind `prober` (a new enum value) and writes its token to a volume that only the prober mounts. The prober reads its work list from `/api/v1/prober/checks` and sends results through the ingest endpoint, exactly like an agent. On restart the API keeps a valid token and replaces an invalid one (revoking the old one).
3. **Probe state is decided on the server, from Prometheus.** The prober only sends results; a background job reads `probe_success` and applies "down = two failures in a row". Stored telemetry is the single source of truth, and a restart of either side loses nothing.
4. **Health of `http_check` integrations.** Their results arrive under the prober's token, so the state job marks the check's own integration as having sent metrics. That gives it the usual healthy / stale status.
5. **The dev allowlist.** The demo shop has only private addresses. `SENTINEL_PROBER_ALLOW_HOSTS` allows exact host names (default `aviro_gateway_1` and the test target), only in development: production refuses to start with it set. The prober reads it from its own environment, never from the server, so a compromised API can't widen it. In dev the prober also joins `sentinel-ingest` to reach the shop.
6. **Labels.** Prometheus now promotes `sentinel.check.id/name/kind/integration_id` and `server.address`.

## Known limitations

- **No "probe now" button yet.** The API design lists `POST /api/v1/integrations/{id}/test` ("run a probe now"). Running a probe from the API would bypass the prober's network isolation, so it should go through the prober. Not built in this step.
- **Probes run from one place:** SENTINEL's own host. A regional or private-network probe runner is a Could item.
- **No alert rules on probe metrics yet.** "Down for 2 checks", latency and certificate-expiry rules come with the rule packs in Step 10. The state and events exist now.
- **DNS failure counts as a failed check.** That's correct for an outage, but it means a typo in a name shows up as "down", not as a configuration error (the API does refuse names that don't resolve at creation).
- **The guard trusts the resolver's answers.** Pinning prevents rebinding within a check. It doesn't defend against a compromised resolver that consistently returns attacker-chosen *public* addresses. That is out of SSRF scope.

## How you verify

```bash
cd "/Users/asadsheikh/Coding/SENTINEL Autonomous AI Site Reliability Engineer"
git checkout step-09-prober
cd backend && uv run pytest -q tests/unit/test_ssrf_guard.py tests/unit/test_prober_checks.py && cd ..
make stack-up-fast && make test-gateway                 # includes the live prober tests
# With the demo shop (see docs/guides/health-checks.md):
cd ../aviro-shop && make up-sentinel TOKEN=sit_...      # then add a check for http://aviro_gateway_1:8000/healthz
podman stop aviro_gateway_1                             # state → down within about a minute
```

## Not in this step (by design)

- Alert rules on probe metrics, and the other event types (Step 10).
- The UI for checks (frontend steps).
- "Probe now".
- Private-network probe runners (Could).
