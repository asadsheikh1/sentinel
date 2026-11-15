# 02 — MoSCoW Feature Matrix

**Complexity scale:** S (≤ 1 day), M (2–4 days), L (1–2 weeks), XL (> 2 weeks), for one developer working with AI assistance.

**Test types:** U = unit, I = integration, A = API, D = database, E = end-to-end, EV = AI evaluation suite, SEC = security test.

Feature IDs are referenced throughout the roadmap ([Roadmap](../4-plan/1-roadmap.md)).

## Summary by priority

| Priority | Count | Features |
|---|---|---|
| **Must** | 52 | A1–A5, A8, B1, B2, B6–B9, C1–C6, C9, D1–D5, F1, F2, G1, H1, H2, I1–I14, P1–P9 |
| **Should** | 29 | A6, A7, B3–B5, C7, D7, E1–E5, F3–F5, G2–G4, H3, I15–I20, I29, P16, P17, P21 |
| **Could** | 18 | A9, B10, B11, C8, D6, E6, F6, F7, G5, I21, I22, I24–I28, P10, P14 |
| **Won't (now)** | 14 | F8, F9, F10, I30–I33, P11, P12, P13, P15, P18, P19, P20 |

*Revision 2:* section **I. Integrations** was added ([14-integration-architecture.md](../2-architecture/2-integrations.md)). A10 (generic OTLP onboarding) is now covered by I1 and I8.

The Must set is a complete, credible product: **multi-source monitoring** (Linux and container hosts, Podman or Docker, via agent, Python apps via OpenTelemetry, public endpoints via probes, deployments via webhooks), detection, an evidence-cited RCA that is aware of deployments and code, approvals, a report, and deployment. Code repair and the further integrations (database, check-ins, uploads, AWS API) are deliberately **Should**. They add value, but the product is credible without them.

---

## A. Application monitoring

| ID | Feature | Description | Value | Priority | Dependencies | Cx | Acceptance criteria | Tests |
|---|---|---|---|---|---|---|---|---|
| A1 | Reference target app ("AVIRO") | Small multi-service app (API gateway, orders, payments, inventory, Celery worker, Postgres, Valkey) with OTel instrumentation and fault-injection switches | Without a realistic system to monitor there is nothing to detect or evaluate | Must | — | L | All services start with compose. Each fault scenario can be toggled by an admin endpoint or a deploy of a tagged commit. App tests pass. | U, I, E |
| A2 | Log collection | OTel SDK logs → OTel Collector → Loki, with `service`, `trace_id`, and `level` labels | Logs are the main evidence for errors | Must | A1 | M | An ERROR log emitted by `orders` can be queried in Loki within 15 s, with the trace_id attached | I |
| A3 | Metrics collection | OTel metrics and exporters → Prometheus | Required for rates, latency, and saturation | Must | A1 | M | `http_server_request_duration` histograms per service and route are visible in Prometheus | I |
| A4 | Trace collection | OTel traces → Collector → Jaeger | Locates the failing hop and affected services | Must | A1 | M | A request through gateway → orders → payments shows as one trace in Jaeger | I |
| A5 | HTTP error and latency (RED) monitoring | Recording rules for request rate, error rate, and p50/p95/p99 latency per service | The core health signal | Must | A3 | S | Recording rules evaluate. Values match a controlled load test within ±5% | I |
| A6 | Background job monitoring | Celery task success, failure, retry, duration, and queue depth metrics | Covers non-HTTP failure modes | Should | A1, A3 | M | Failed tasks increment a counter. Queue depth is exported | I |
| A7 | Database performance monitoring | postgres_exporter (connections, locks, slow queries) plus DB client spans | Diagnoses pool exhaustion and slow-query incidents | Should | A3, A4 | M | Connection count and DB span durations are visible. Pool exhaustion is detectable in the scenario | I |
| A8 | Service health checks | `/healthz` and `/readyz` on every service, scraped as `up` | Basic liveness | Must | A1 | S | A stopped container produces `up == 0` within 30 s | I |
| A9 | Grafana dashboards | Provisioned dashboards for operators | Nice for demos. SENTINEL's UI is the primary UI | Could | A3–A4 | S | Dashboards load from provisioning files | Manual |
| A10 | ~~Generic OTLP onboarding~~ | *Superseded by I1 and I8 (revision 2)* | — | — | — | — | — | — |

## B. Incident detection

| ID | Feature | Description | Value | Priority | Dependencies | Cx | Acceptance criteria | Tests |
|---|---|---|---|---|---|---|---|---|
| B1 | HTTP 5xx detection | Prometheus alert rule: 5xx ratio > threshold for N minutes per service | The most common incident type | Must | A5, B7 | S | The scenario raises an alert within ≤ 3 min. No alert under fault-free load | I, EV |
| B2 | Latency spike detection | p95 above both a static threshold and 2× the 1h baseline | Catches degradation without errors | Must | A5 | S | The slow-query scenario raises an alert. A normal traffic spike does not | I, EV |
| B3 | Failed background job detection | Alert on task failure rate or growth in the dead-letter queue | Covers worker incidents | Should | A6 | S | The worker-failure scenario is detected | I, EV |
| B4 | DB connection failure detection | Alert on connection errors and pool saturation (metrics plus log fingerprint) | A frequent real-world cause | Should | A7, B6 | S | The pool-exhaustion scenario is detected | I, EV |
| B5 | Resource usage detection | CPU, memory, and restart-count alerts from cAdvisor and node metrics | Detects leaks and OOM | Should | A3 | S | The memory-leak scenario is detected before or at the first OOM restart | I, EV |
| B6 | Log error grouping | Poll Loki for ERROR logs. Fingerprint (exception type + normalised message + top in-app frame). Track counts | Turns log noise into countable groups. Detects new errors | Must | A2 | M | The same error with different IDs maps to one fingerprint. A new fingerprint emits a signal | U (property tests), I |
| B7 | Signal correlation into incidents | Merge signals by service, time window, shared trace IDs, and deployment proximity. Deduplicate | One problem becomes one incident, not 20 | Must | B1/B6 | M | One fault produces exactly 1 incident with ≥ 2 linked signals. Two unrelated faults produce 2 incidents | U, I, EV |
| B8 | Severity assignment | Rule table (error ratio, user-facing service, breadth) → SEV1–SEV4. Severity can only be escalated automatically | Prioritisation | Must | B7 | S | Rule table unit-tested. Changes are recorded on the timeline | U |
| B9 | Incident timeline | Append-only events: signals, deploys, investigation steps, actions, status changes | Core to investigation and reports | Must | B7 | S | Every state change produces a timestamped event. API returns the events ordered | U, A, D |
| B10 | Statistical anomaly detection | EWMA or seasonal baseline per metric | Fewer hand-tuned thresholds | Could | A5 | M | Lower FP rate than static rules on the scenario suite | EV |
| B11 | Notifications | Email or Slack/Discord webhook on new SEV1–SEV2 incidents | Real-world usability | Could | B7 | S | A webhook is received with an incident link | I |

## C. AI root-cause analysis

| ID | Feature | Description | Value | Priority | Dependencies | Cx | Acceptance criteria | Tests |
|---|---|---|---|---|---|---|---|---|
| C1 | Deterministic evidence collection | For an incident, collect log excerpts, metric series, exemplar traces, and error groups. Store each as immutable evidence with an ID | Evidence-based investigation is the core principle | Must | B7, adapters | L | Each scenario incident has ≥ 1 log, ≥ 1 metric, and ≥ 1 trace evidence item. Payloads are redacted | U, I, EV |
| C2 | Deployment tracking | Deployment events (service, version, commit SHA, time) through a webhook from CI or the demo deploy script | "What changed?" is the first SRE question | Must | P3 | S | A deployment appears on the timeline and in the API with its SHA | A, D |
| C3 | Before/after deployment comparison | Compare error rate, latency, and error groups in windows before and after the nearest deploy | Strong causal signal | Must | C1, C2 | M | The bad-deploy scenario produces a comparison evidence item showing the change. Statistics are unit-tested | U, EV |
| C4 | Affected service identification | Derive affected and upstream services from trace error propagation | Narrows the investigation | Must | A4, C1 | M | The downstream-timeout scenario identifies the origin service correctly | U, EV |
| C5 | Ranked hypotheses with confidence | LLM produces up to 5 hypotheses (statement, mechanism, cited evidence IDs, confidence label) plus a deterministic evidence score | The main product output | Must | C1, P5 | L | Schema-valid output. Ranking metrics are recorded per scenario | U (fake LLM), EV |
| C6 | Citation validation | The server rejects hypotheses that cite missing or foreign evidence, then re-prompts. Uncited hypotheses are dropped | Prevents fabricated evidence | Must | C5 | S | 100% of persisted hypotheses have valid citations. Unit tests with adversarial outputs | U, EV |
| C7 | Live investigation progress | SSE stream of step events to the UI | UX and transparency | Should | C5 | S | The UI shows steps as they complete. Reconnect resumes | A, E |
| C8 | Interactive follow-up questions | Ask a question about an incident. Answers are grounded in the stored evidence | Exploration | Could | C5 | M | Answer cites evidence | EV |
| C9 | Human hypothesis feedback | Confirm or reject a hypothesis, or record an actual root cause | Ground truth for memory and evaluation | Must | C5 | S | Feedback is stored, audited, and shown on the timeline | A, D |

## D. AI software investigation

| ID | Feature | Description | Value | Priority | Dependencies | Cx | Acceptance criteria | Tests |
|---|---|---|---|---|---|---|---|---|
| D1 | GitHub repo connection (read-only) | Fine-grained PAT or GitHub App, read-only. Bare mirror clone kept locally | Code evidence | Must | P1 | M | Repo synced. Token stored encrypted and never logged or sent to the LLM | I, SEC |
| D2 | Commit-window inspection | Commits and changed files between the previous and current deployed SHAs | Links incidents to changes | Must | D1, C2 | S | The bad-deploy scenario lists the culprit commit | U, I |
| D3 | Stack-trace → source mapping | Parse Python tracebacks from logs and map them to repo files, lines, and functions at the deployed SHA | Precise code evidence | Must | D1, B6 | M | Frames resolve to the correct file and line for the scenario traces | U |
| D4 | Code search | ripgrep-based keyword and symbol search at a given SHA, with a size-limited excerpt | Finds related code | Must | D1 | S | Returns ranked excerpts. Path traversal is impossible | U, SEC |
| D5 | Suspicious change ranking | Score commits in the window by overlap with stack-trace files, error-group functions, and touched config. LLM explains the top diffs | Pinpoints the culprit commit | Must | D2–D4, C5 | M | Culprit commit is ranked #1 in ≥ 80% of bad-deploy scenarios (measured) | U, EV |
| D6 | Semantic code search | Embedding index of code chunks | Better recall for vague errors | Could | D4, G2 | M | Improves evidence coverage on the suite | EV |
| D7 | Suggested fix (prose) | Natural-language fix recommendation attached to the top hypothesis | Guides humans even without the repair feature | Should | D5 | S | Present for code-rooted scenarios | EV |

## E. AI code repair

| ID | Feature | Description | Value | Priority | Dependencies | Cx | Acceptance criteria | Tests |
|---|---|---|---|---|---|---|---|---|
| E1 | Sandbox test runner | Ephemeral rootless Podman container: no network, read-only rootfs, non-root, capabilities dropped, CPU, memory, and PID limits, timeout | Safe execution of untrusted code | Should | Runner service | L | The security test suite proves no network, no host mount, limits enforced, and timeout kill | I, SEC |
| E2 | Patch generation | LLM produces a unified diff for the target files with an explanation | Speeds up the fix | Should | D5 | L | Diff applies cleanly (`git apply --check`) in ≥ X% of scenarios (measured) | U, EV |
| E3 | Test execution | Run the repo's test command from `.sentinel.yml` (allowlisted) against the patched tree | Validates the patch | Should | E1, E2 | M | Results parsed (passed, failed, errors) and stored | I |
| E4 | Bounded revise loop | Feed test failures back. At most 3 attempts | Higher pass rate | Should | E3 | M | Loop stops at success or the limit. Each attempt is stored | U, EV |
| E5 | Diff display and explanation | UI diff viewer with test output | Human review | Should | E2 | S | Diff renders. Explanation is shown | E |
| E6 | Regression test generation | LLM writes a failing-before, passing-after test | Prevents recurrence | Could | E4 | M | Generated test fails on the original and passes on the patch | EV |

## F. Autonomous incident response

| ID | Feature | Description | Value | Priority | Dependencies | Cx | Acceptance criteria | Tests |
|---|---|---|---|---|---|---|---|---|
| F1 | Approval workflow framework | Action requests with risk level, policy (who may approve, expiry), approve or reject, idempotent executor, audit | Safety backbone for every action | Must | P1, P2 | M | Unapproved actions cannot execute (tested). Expired approvals rejected. Self-approval blocked for high-risk actions | U, A, SEC |
| F2 | Create GitHub issue | Issue with incident summary and evidence links | Low-risk, useful | Must | F1, D1 | S | Issue created after approval. URL stored on the timeline | I (mock GitHub) |
| F3 | Restart service | Restart an allowlisted container (demo) or ECS service | Common mitigation | Should | F1, Runner | M | Only allowlisted targets. Result audited | I, SEC |
| F4 | Roll back deployment | Redeploy the previous known-good image tag for an allowlisted service | Most effective mitigation for bad deploys | Should | F1, C2, Runner | M | Rollback scenario resolves the incident. Target version must exist in deployment history | I, E |
| F5 | Draft pull request | Push patch branch and open a **draft** PR (requires a write-scoped token, used only by the executor) | Hand-off to humans | Should | E4, F1 | M | Draft PR created. Never merged | I (mock GitHub) |
| F6 | Retry failed jobs | Requeue failed Celery tasks by ID | Worker recovery | Could | F1, A6 | S | Requeued count reported | I |
| F7 | Clear failed queue | Purge the dead-letter queue (destructive, high risk, two-person approval) | Ops completeness | Could | F1 | S | Requires 2 distinct approvers | U, SEC |
| F8 | Unattended auto-remediation | Execute actions without approval | — | **Won't** | — | — | Deliberately excluded for safety | — |
| F9 | Auto-merge or auto-deploy of patches | — | — | **Won't** | — | — | Explicitly prohibited | — |
| F10 | General runbook automation engine | Arbitrary scripted runbooks | — | **Won't** | — | — | Out of scope. Too broad an attack surface | — |

## G. Incident memory

| ID | Feature | Description | Value | Priority | Dependencies | Cx | Acceptance criteria | Tests |
|---|---|---|---|---|---|---|---|---|
| G1 | Store incidents, root causes, resolutions | Resolution record (confirmed cause, actions, linked commits or PRs) on close | Organisational memory | Must | C9 | S | Cannot close without a resolution summary (configurable). Persisted and audited | A, D |
| G2 | Embeddings and similarity search | Local embedding model. pgvector HNSW index over incident summaries and resolutions | Re-use of past fixes | Should | G1, P5 | M | Seeded near-duplicate incident retrieved at top-3 | U, D, EV |
| G3 | History-aware investigations | Similar incidents become evidence items in new investigations | Faster RCA | Should | G2, C5 | S | Recurrence scenario cites the prior incident | EV |
| G4 | Resolution code linkage | Store fixing commit or PR SHA with the resolution | Traceability | Should | G1, D1 | S | Shown in report and similarity results | A |
| G5 | Import external postmortems | Markdown postmortems ingested as memory | Bootstraps memory | Could | G2 | S | Imported docs retrievable | I |

## H. Incident reporting

| ID | Feature | Description | Value | Priority | Dependencies | Cx | Acceptance criteria | Tests |
|---|---|---|---|---|---|---|---|---|
| H1 | Incident report generation | Facts (times, severity, services, evidence, actions, status) filled deterministically. LLM writes the summary narrative citing evidence | Communication | Must | B9, C5 | M | Report contains all required sections. Facts match the DB exactly | U, EV |
| H2 | Markdown export | Download the report as `.md` | Shareable | Must | H1 | S | Valid Markdown. Matches the stored report | A |
| H3 | Prevention recommendations | LLM-generated, evidence-grounded prevention items | Learning | Should | H1 | S | Each recommendation references a hypothesis or evidence item | EV |

## I. Integrations (revision 2)

See [14-integration-architecture.md](../2-architecture/2-integrations.md) for the method-by-method visibility analysis.

| ID | Feature | Description | Value | Priority | Dependencies | Cx | Acceptance criteria | Tests |
|---|---|---|---|---|---|---|---|---|
| I1 | Common OTLP ingestion gateway | Public `ingest.` endpoint: Caddy TLS + forward_auth → gateway OTel Collector → Prometheus, Loki, Jaeger | One pipeline for every source. No per-platform implementations | Must | P1, P3 | M | All pushed telemetry flows only through the gateway | I |
| I2 | Integration registry and scoped ingest tokens | Integrations (kind, environment, config). Write-only tokens: create, rotate, revoke | Secure onboarding and attribution | Must | P1 | S | Revoked token → 401 within 30 s. Token cannot read any data | A, SEC |
| I3 | Normalisation and enrichment | Identity attributes set from the token (client values overwritten). OTTL rules to semantic conventions. Redaction. Cardinality guard | Uniform data for detection and AI | Must | I1 | M | Spoofed integration ID overwritten. Variant attributes normalised (fixture suite) | U, I, SEC |
| I4 | Integration health and staleness | Last-seen per integration and signal. States pending, healthy, degraded, stale. `telemetry_gap` signal with disambiguation | Knowing when monitoring itself is broken | Must | I1 | M | Stop the agent → `stale` and signal within 5 min (+ interval). Disambiguation table tested | U, I |
| I5 | SENTINEL Agent packaging and installer | Pinned OTel Collector + SENTINEL config templates. Signed or checksummed install script (Ubuntu/Debian, Amazon Linux 2023). systemd unit, non-root user, container variant | Monitor any Linux machine: EC2, other clouds, on-prem | Must | I1, I2 | M | Fresh VM → healthy in ≤ 2 min. Clean uninstall | I (VM/container) |
| I6 | Host, process, and log collection | hostmetrics, watched processes, journald, file logs with multiline | Inside-out host visibility | Must | I5 | M | CPU, memory, disk, process presence, journal, and app log files queryable per host | I |
| I7 | Container collection (**Podman and Docker**) | `docker_stats` receiver against the Podman or Docker API through a GET-only socket proxy (or `podman_stats`). Container log files and journald. Compose labels → service names | Container hosts and Compose apps. Podman is required because AVIRO runs on it | Must | I5 | S | Container restart and OOM visible. Socket proxy denies POST | I, SEC |
| I8 | Python app OTel instrumentation | Auto-instrumentation guide and validated config (to agent or gateway). The demo uses it | Requests, traces, and correlated logs | Must | I1 | S | AVIRO traces and logs arrive with correct `service.name` | I |
| I9 | External health checks (prober) | HTTP(S), TCP, TLS-expiry probes from an isolated, SSRF-guarded prober | No-agent monitoring and outage detection | Must | I1 | M | SSRF suite 100% blocked. Endpoint-down detected in ≤ 2 intervals | U, I, SEC |
| I10 | Deployment and GitHub webhooks | Generic deployment webhook (any CI) plus GitHub webhooks (HMAC) → `SentinelEvent` | Change context for RCA | Must | P3 | S | Signed events accepted, forged rejected, idempotent | A, SEC |
| I11 | Rule packs per entity type | Label-generic Prometheus rules for host, container, service, database, endpoint, integration | Detection applies automatically to any onboarded entity | Must | I3 | M | `promtool` tests for every rule. Faults detected per scenario | U, EV |
| I12 | Entity inventory and topology-aware correlation | Entities discovered from resource attributes. Relations (runs_on, uses, fronts, built_from). Correlator uses them | Cross-layer incidents (host disk full → service errors) | Must | I3, B7 | M | Topology scenario → 1 incident with both entities | U, EV |
| I13 | Integrations catalogue and setup wizard | Tiles, configuration, token shown once, tailored instructions, live verification checklist (incl. end-to-end log marker) | The onboarding experience | Must | I2, I4, P7 | M | A user onboards a VM and an HTTP check from the UI alone | E |
| I14 | Inventory and integration health dashboard | Entities, relations, last seen, per-signal status, stale warnings | Trust that monitoring works | Must | I4, I12 | S | Status changes reflected within 30 s | E |
| I15 | PostgreSQL integration | `postgresql` receiver in the agent, `pg_monitor` setup helper, DB rule pack | Database incident coverage | Should | I5 | S | Pool-exhaustion scenario visible and detected | I, EV |
| I16 | Job and cron check-ins | Check-in monitors with schedule and grace period. Missed, late, or failed → signal | Scheduled jobs on any platform | Should | I2 | S | Missed run detected within grace (freezegun tests) | U, A |
| I17 | Node.js instrumentation | Guide plus a Node service in AVIRO | Polyglot proof | Should | I8 | S | Cross-language trace visible | I |
| I18 | Manual diagnostic uploads | Upload logs and exports → parsed, redacted evidence bundle → manual investigation | Investigation when no monitoring exists | Should | C5 | M | Zip bomb and traversal blocked. Sample bundle → cited hypotheses | U, SEC, EV |
| I19 | Pull connector framework | `Connector` interface: test, discover, poll (stream), query_for_investigation (on demand) | API integrations without bespoke pipelines | Should | I1, C1 | M | Interface conformance tests | U |
| I20 | AWS account connector | Read-only cross-account role (ExternalId). EC2 inventory and status, key CloudWatch metrics, CloudTrail change events | Outside-in cloud view, change events, gap disambiguation | Should | I19 | M | Real account connected. Stop instance → change event + correct gap interpretation | U (moto), manual I |
| I21 | Kubernetes | Collector Helm values (DaemonSet + cluster receivers), tested on kind | Large ecosystem | Could | I5 | M | Pod crash-loop detected on kind | I |
| I22 | MySQL integration | `mysql` receiver + rule pack | Breadth | Could | I5 | S | Metrics visible | I |
| I23 | ~~Podman support~~ | *Merged into I7 (Must). SENTINEL itself runs on Podman* | — | — | — | — | — | — |
| I24 | Datasource mode | Query an existing customer Prometheus or Loki instead of ingesting | Fits teams with existing stacks | Could | Adapters | M | Investigation works against an external Prometheus | I |
| I25 | Generic third-party alert webhooks | Grafana, Datadog, New Relic alert payload mapping | Use existing alerting | Could | I10 | S | Mapped fixture payloads | A |
| I26 | OpAMP remote agent management | Remote config and status via OpAMP supervisor | Fleet operations | Could | I5 | L | Config change applied remotely, audited | I |
| I27 | Private-network probe runner | Prober deployable inside a private network | Internal endpoints | Could | I9 | S | Internal check works with no inbound access | I |
| I28 | AWS API actions | Reboot instance or SSM restart service via separate write role, approval required | Remediation beyond the demo | Could | I20, F1 | M | Only after approval. Role scoped to tagged resources | I, SEC |
| I29 | Slim custom Collector build | OCB build with only the needed components, signed in CI | Smaller binary, less attack surface | Should | I5 | S | Binary size and component list published. Passes agent tests | I |
| I30 | Own agent implementation | — | — | **Won't** | — | — | OTel Collector is used instead ([ADR-0013](../adr/0013-otel-collector-as-agent.md)) | — |
| I31 | Remote command execution through the agent | — | — | **Won't** | — | — | The agent is data-out only ([ADR-0016](../adr/0016-data-only-agent.md)) | — |
| I32 | Azure and GCP API connectors | — | — | **Won't** | — | — | The agent covers their VMs. Connectors later via I19 | — |
| I33 | Windows host installer | — | — | **Won't** | — | — | Collector supports Windows, untested | — |

## P. Platform and cross-cutting

| ID | Feature | Description | Value | Priority | Dependencies | Cx | Acceptance criteria | Tests |
|---|---|---|---|---|---|---|---|---|
| P1 | Authentication and RBAC | Local accounts (Argon2id), session cookie, roles viewer / responder / approver / admin | Security baseline | Must | — | M | Permission matrix enforced on every endpoint (table-driven tests) | U, A, SEC |
| P2 | Audit log | Append-only record of all security-relevant and state-changing operations, with hash chaining | Accountability | Must | — | S | No UPDATE or DELETE grant on the table. Chain verification passes | D, SEC |
| P3 | Ingestion API keys | Hashed, scoped keys for webhooks (alerts, deployments) | Machine authentication | Must | P1 | S | Wrong or absent key → 401. Scope enforced | A, SEC |
| P4 | Secret redaction | Redact tokens, keys, passwords, emails, and high-entropy strings before storage as evidence and before any LLM prompt | Never expose secrets to the model | Must | — | M | Redaction corpus test: 100% of seeded secrets removed | U, SEC |
| P5 | LLM gateway and telemetry | Single client for Ollama: structured output, timeouts, retries, token, latency, and model logging | Reliability and cost measurement | Must | — | M | Every call logged with tokens and latency. Timeouts tested | U, I |
| P6 | Evaluation harness and scenario suite | Replayable scenarios with ground truth. Metric computation. Results tracked in the repo | Measurable AI quality | Must | A1, C5 | L | `make eval` produces a metrics report per model and commit | EV |
| P7 | Web UI | React SPA: incidents, investigation, evidence, approvals, reports, admin | Product surface | Must | API | L | E2E happy path passes | E |
| P8 | AWS deployment | IaC-provisioned EC2 stack with TLS, backups, and least-privilege IAM | Public portfolio demo | Must | All core | L | Fresh `tofu apply` plus deploy workflow gives a working HTTPS site | Manual, E (smoke) |
| P9 | CI pipelines | Lint, type check, tests, build, security scans on PRs | Quality gate | Must | — | M | All workflows green on main | CI |
| P10 | SSO / OIDC login | GitHub OAuth or OIDC | Convenience | Could | P1 | M | Login via provider | A |
| P11 | Multi-tenancy | — | — | **Won't** | — | — | — | — |
| P12 | Kubernetes deployment | — | — | **Won't** | — | — | Podman Compose on EC2 is sufficient and cheaper | — |
| P13 | GPU inference on AWS | — | — | **Won't** | — | — | Cost. Revisit after v1.0 | — |
| P14 | MCP server exposing SENTINEL (read-only) | Lets IDE agents query incidents and evidence | Interop showcase *after* the core works | Could | C5 | S | MCP inspector lists tools. Read-only verified | I |
| P15 | Model fine-tuning | — | — | **Won't** | — | — | Not needed. Evaluate prompts and retrieval first | — |
| P16 | SENTINEL self-monitoring | SENTINEL's own OTel traces and metrics, health alerts | Dogfooding and ops | Should | A2–A4 | S | SENTINEL's API and worker visible in Jaeger and Prometheus | I |
| P17 | Backup and restore | Nightly `pg_dump` to S3. Restore procedure tested | Data safety | Should | P8 | S | Restore drill documented and executed | Manual, I |
| P18 | Mobile app | — | — | **Won't** | — | — | — | — |
| P19 | GitLab / Bitbucket support | — | — | **Won't** | — | — | Interface designed so it can be added later | — |
| P20 | Optional paid-LLM provider plugins | — | — | **Won't** | — | — | Conflicts with project constraints | — |
| P21 | Rate limiting and abuse protection | Login and API rate limits | Public demo hardening | Should | P1 | S | Brute-force test blocked | SEC |
