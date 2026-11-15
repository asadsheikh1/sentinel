# 01 — Product Requirements Document (PRD)

| Field | Value |
|---|---|
| Product | SENTINEL — Autonomous AI Site Reliability Engineer |
| Document status | **Draft for approval** |
| Version | 0.2 (planning phase, revision 2: integration architecture) |
| Date | 2026-11-14 |

---

## 1. Problem statement

When a production service degrades, on-call engineers lose most of their time on *investigation*, not on fixing. They jump between log search, metric dashboards, trace viewers, deployment history, and source code. They try to rebuild a timeline, work out what changed, and remember whether this has happened before. That context-switching is slow and error-prone, and it depends heavily on whoever happens to be on call.

Existing "AI ops" products often stop at summarising alerts or wrapping a chatbot around a dashboard. Their conclusions are hard to trust because they **do not show the evidence**.

## 2. Product vision

SENTINEL acts like a careful SRE colleague. It:

1. **Connects** to the systems it monitors through a small set of integrations: an installable agent for Linux and container (Podman or Docker) hosts, OpenTelemetry for applications, external health checks, webhooks, cloud API connectors, job check-ins, and manual uploads. All of them feed one common ingestion pipeline ([Integrations](../2-architecture/2-integrations.md)).
2. **Watches** logs, metrics, traces, and change events from servers, containers, applications, databases, endpoints, and pipelines.
3. **Detects** abnormal behaviour and groups related signals into a single incident.
4. **Investigates** automatically. It collects evidence from telemetry, deployments, source code, and past incidents.
5. **Explains** the likely root causes as a ranked list. **Every claim cites concrete evidence items** that a human can open and check.
6. **Proposes** remediations: an operational action, a code patch tested in a sandbox, or a GitHub issue or PR.
7. **Acts only with human approval** for anything sensitive. It never merges or deploys by itself.
8. **Remembers** resolved incidents so that later investigations get better.

The defining principle is **evidence-based investigation**. A hypothesis without valid evidence citations is rejected by the system before a user ever sees it.

## 3. Goals and non-goals

### Goals (initial release, v1.0)
- G1. Detect injected faults across **several environment types** (Linux host, container host, Python services, public endpoints), with measured precision and recall.
- G1b. Onboard a new Linux machine (EC2, other cloud, on-prem, or local VM) or public endpoint **in under 5 minutes using only the UI instructions**, with a live verification checklist.
- G2. For each incident, produce ranked root-cause hypotheses in which every claim is linked to stored evidence.
- G3. Correlate incidents with deployments and specific commits.
- G4. Run 100% of AI inference locally or self-hosted, with no paid AI APIs.
- G5. Require explicit, audited human approval before any state-changing action.
- G6. Run untrusted code (patch tests) only in a hardened, network-isolated sandbox.
- G7. Deploy publicly on low-cost CPU-only AWS infrastructure.
- G8. Publish a reproducible evaluation methodology and real measured results.
- G9. Use one integration architecture: every source is normalised into OTLP telemetry or `SentinelEvent` events. There is no platform-specific monitoring pipeline.
- G10. Report the health of every integration and detect when monitoring itself stops working.

### Non-goals (v1.0)
- Replacing a full commercial observability suite (Datadog, New Relic, and similar).
- Monitoring large production estates at scale. The MVP's measured capacity target is about 10 hosts, 20 services, and 50 probes on one instance.
- Building a proprietary monitoring agent. The OpenTelemetry Collector is used instead.
- Executing commands on monitored hosts. The agent is data-out only.
- Autonomous remediation without a human (explicitly excluded).
- Multi-tenant SaaS, billing, and organisation management. Each deployment serves one organisation, and users are invited by an admin.
- Kubernetes-native operation.
- Fine-tuning or training models.

## 4. Personas

| Persona | Needs | How SENTINEL helps |
|---|---|---|
| **On-call engineer (Responder)** | Find out quickly what is broken and why | Incident view, evidence-cited hypotheses, suggested actions |
| **Service owner / developer** | Find out which change caused the problem and how to fix it | Suspicious-commit ranking, code findings, sandbox-tested patch, draft PR |
| **Approver (tech lead / SRE lead)** | Control over risky actions | Approval queue that shows exact action parameters, risk level, and audit trail |
| **Platform / ops engineer (Integrator)** | Connect servers, containers, apps, endpoints, repos, and cloud accounts quickly and safely | Integrations catalogue, guided setup, scoped tokens, live verification, integration health |
| **Admin** | Configure services, repos, users, and credentials | Admin settings, RBAC, API keys |
| **Portfolio reviewer** (hiring manager, interviewer) | See engineering depth and honest results | Live demo, architecture docs, ADRs, evaluation report with real numbers |

## 5. Core user journeys

0. **Connect a system.** The integrator opens *Integrations → Add → Linux host*, names it `shop-vm-1`, and copies the generated install command onto an Ubuntu VM (EC2 or on-prem). Within two minutes the checklist shows *token accepted ✓, host metrics ✓, journal logs ✓, verification marker ✓*. The host and its containers appear in the inventory, and host rule packs apply automatically. They add an HTTP check for the public URL and a deployment webhook to CI.
1. **Detect → investigate → explain.** A bad deploy to the demo `orders` service starts returning HTTP 500s. Within the detection window an incident opens with severity SEV2. An investigation runs automatically. The incident page shows: the timeline, the error group, the 5xx rate before and after the deployment, a failing trace, the commit that touched the failing function, and a top hypothesis such as "KeyError introduced in commit `abc123` in `orders/pricing.py:42`". The hypothesis is backed by evidence chips E-3, E-7, and E-9.
2. **Approve an operational action.** SENTINEL proposes "Roll back `orders` to image `orders:1.4.2`". A Responder requests it, an Approver reviews the exact parameters and approves, the privileged runner executes it, and the result and audit record are attached to the incident timeline.
3. **Repair code.** The developer clicks "Propose patch". SENTINEL generates a diff, runs the repository's test suite in an isolated container, revises the patch if tests fail (bounded attempts), and shows the final diff with test output. With approval, it opens a **draft** PR.
4. **Close and learn.** The responder confirms the root cause and records the resolution. SENTINEL generates a report and stores an embedding of the incident. The next similar incident shows "Similar past incident: INC-12 (resolved by rollback)" as evidence.

## 6. Functional requirements (summary)

The full, prioritised list is in [02-moscow-feature-matrix.md](2-features-moscow.md). The headline areas are:

- **Integrations:** a common OTLP ingestion gateway with per-integration tokens. Normalisation and entity discovery. Integration health and staleness. A SENTINEL Agent (packaged OpenTelemetry Collector) for Linux and container hosts (Podman and Docker). Python OTel instrumentation. External health checks. Deployment, GitHub, and Alertmanager webhooks. *Should:* PostgreSQL, job check-ins, Node.js, manual uploads, AWS account connector.
- **Monitoring:** logs, metrics, and traces from hosts, containers, and services, with rule packs per entity type.
- **Detection:** rule-based alerting plus log error fingerprinting, correlation into incidents, severity, and timeline.
- **Root-cause analysis:** deterministic evidence collection, then LLM hypothesis generation with server-validated citations.
- **Code investigation:** a GitHub repo mirror, commit-window analysis, stack-trace mapping, code search, and suspicious-change ranking.
- **Code repair:** patch generation, sandboxed tests, a bounded revise loop, and diff display.
- **Response:** a policy-driven approval workflow, a small allowlisted set of actions, and a full audit trail.
- **Memory:** resolutions stored with embeddings and retrieved by similarity.
- **Reporting:** structured incident reports with Markdown export.

## 7. Non-functional requirements

| Category | Requirement |
|---|---|
| **Cost** | No paid AI API. AWS MVP should cost about $150/month or less when always on (see [AWS & costs](../4-plan/2-aws-and-costs.md)). |
| **Privacy** | Telemetry and code never leave the deployment. Secrets are redacted before any LLM call. |
| **Security** | RBAC, audit log, human approval, sandbox isolation, and no LLM access to the host shell (see [Security](../2-architecture/6-security.md)). |
| **Integration security** | Ingest tokens are write-only and revocable. Identity attributes are set by the gateway, not by clients. The prober is SSRF-guarded. Cloud access is read-only role assumption with no static keys |
| **Agent footprint** | Measured and published. Provisional target ≤ 150 MB RAM and ≤ 2% of one CPU on a typical VM |
| **Integration freshness** | A stopped source is detected as `stale` within 5 min (configurable) |
| **Reliability** | Investigations are checkpointed and resumable after a worker crash. Ingestion is idempotent. |
| **Performance (local, M4)** | Time to first hypothesis is *measured*. The provisional target is ≤ 3 minutes for standard scenarios with the default model. |
| **Performance (AWS CPU)** | Investigations are asynchronous. The provisional target is ≤ 15 minutes with the small CPU model, to be validated in Step 15 and Step 33. |
| **Explainability** | 100% of displayed hypotheses cite at least one existing evidence item (enforced by the server). |
| **Reproducibility** | Evaluation scenarios are replayable from stored telemetry fixtures, so results do not depend on live timing. |
| **Portability** | `podman compose up` plus native Ollama runs the full stack on macOS (Apple Silicon, via `podman machine`) and Linux arm64/amd64. No Docker required. |
| **Maintainability** | Typed Python (mypy strict on core packages), ≥ 80% line coverage on non-LLM core modules, ADRs for major decisions. |

## 8. Constraints

- Development and inference hardware: MacBook M4, 24 GB unified memory. This limits the local model to roughly 14B parameters at 4-bit quantisation while the observability stack is also running.
- Only open-weight models, run through Ollama (or llama.cpp, which is compatible).
- AWS is used for hosting only. There is no GPU in the MVP, and no free tier is assumed.
- Open-source dependencies, with licence restrictions documented (see [Risks & licences](../4-plan/4-risks-and-licences.md)).

## 9. Assumptions

- Monitored systems can reach SENTINEL's ingest endpoint over **outbound HTTPS**, or are reachable by probes or a cloud API. Agents need Linux (Ubuntu/Debian or Amazon Linux 2023 in the MVP) and systemd, or a container runtime (Podman or Docker).
- The source code of monitored applications is hosted on GitHub.
- A single team operates the deployment (single tenant).
- Investigation latency of minutes, rather than seconds, is acceptable because the AI runs asynchronously alongside human responders.

## 10. Success metrics

Exact definitions are in [08-testing-and-evaluation.md](../3-quality/1-testing-and-evaluation.md). All numbers are **measured during development, not assumed**.

| Metric | Provisional v1.0 target (to be validated) |
|---|---|
| Incident detection recall (scenario suite) | ≥ 0.90 |
| Incident detection precision | ≥ 0.85 |
| False-positive incidents per hour of fault-free load | ≤ 0.1 |
| Time from integration creation to first data (Linux host, guided) | ≤ 5 min (measured in a usability dry run) |
| Stale-integration detection time | ≤ 5 min + 1 poll interval |
| Root-cause top-1 accuracy | ≥ 0.60 |
| Root-cause top-3 accuracy | ≥ 0.80 |
| Citation validity (cited evidence exists and belongs to the incident) | 100% (enforced) |
| Evidence coverage (required evidence cited) | ≥ 0.70 |
| Tool execution success rate | ≥ 0.95 |
| Patch test pass rate (repairable scenarios) | Measured; no target until a baseline exists |

## 11. Release plan

| Release | Contents | Roadmap steps |
|---|---|---|
| v0.1 "Detect & Explain" | Demo app, ingestion gateway, agent, probes, integrations UI, detection, incidents, RCA with evidence | 1–19 |
| v0.2 "Code-aware" | GitHub integration, code investigation, memory, reports, approvals | 20–24 |
| v0.3 "Repair & Integrations+" | PostgreSQL, check-ins, Node.js, uploads, AWS connector, runner actions, code repair, draft PRs (all Should) | 25–31 |
| v1.0 "Deployed" | Security hardening, AWS infrastructure, CD, operational readiness, published evaluation | 32–35 |

## 12. Glossary

- **Signal:** a raw detection input, either a fired Prometheus alert or a new or spiking error group.
- **Incident:** a correlated group of signals that represents one problem, with a lifecycle and timeline.
- **Evidence:** an immutable, stored snapshot of data (a log excerpt, metric series, trace, deployment, diff, or past incident) with an ID such as `E-17`.
- **Hypothesis:** a candidate root cause that cites evidence IDs, with a rank and confidence.
- **Action request:** a proposed state-changing operation that is subject to an approval policy.
- **Integration:** a configured connection through which SENTINEL receives data (agent, OTel app, probe, webhook, connector, check-in, upload). It has its own credentials and health status.
- **Entity:** a monitored thing (host, container, service, database, endpoint, job, cloud resource, repository), discovered from telemetry or created by an integration, and linked to others by relations.
- **SENTINEL Agent:** a pinned, SENTINEL-configured OpenTelemetry Collector installed on a monitored machine. It only sends data.
- **SentinelEvent:** the canonical schema for discrete events (deployments, alerts, check-ins, cloud changes, probe and integration state changes).
- **Runner:** the privileged broker service. It is the only SENTINEL component with access to the container engine (rootless Podman), and it exposes a small allowlisted API.
