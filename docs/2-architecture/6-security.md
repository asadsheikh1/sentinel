# 07 — Security Architecture

Security is a product requirement. SENTINEL reads attacker-influenceable data (logs, code, commit messages), runs untrusted code (patch tests), and can change production state (restarts, rollbacks). Each of these is a distinct trust boundary.

## 1. Threat model (summary)

| # | Threat | Vector | Primary controls |
|---|---|---|---|
| T1 | **Prompt injection** makes the AI recommend or perform harmful actions | Crafted log message, exception text, code comment, commit message, HTTP header captured in a trace | LLM has no state-changing tools or credentials. Outputs are schema- and catalog-validated. Actions require human approval with the exact parameters shown. Untrusted-content delimiting. Suspicious-content flags. Injection evaluation scenario |
| T2 | **Sandbox escape or abuse** by a malicious test or patch | Repo code executed in tests | Runner-only access to the rootless Podman API, `network none`, non-root, `cap_drop ALL`, read-only rootfs, resource limits, timeout, no socket or host mounts, gVisor on AWS (Should) |
| T3 | **Secret leakage to the model or UI** | Secrets in logs, env dumps, config files | Redaction before evidence storage and before prompts. Credentials table never read by LLM code paths. Sandbox gets no secrets |
| T4 | **Unauthorised destructive action** | Compromised account, CSRF, insider, replay | RBAC, approval policies, separation of duties, expiry, idempotency keys, CSRF tokens, audit hash chain |
| T5 | **Privilege escalation through the runner** | Worker compromised, then asks the runner for arbitrary container operations | The runner exposes only fixed operations. Targets come from the runner's *own* allowlist file. HMAC with nonce and timestamp. Internal network only |
| T6 | **Stored XSS** through logs or LLM output rendered in the UI | `<script>` in log lines | React text rendering only. Markdown with HTML disabled. Strict CSP |
| T7 | **Credential stuffing or brute force** | Public login | Argon2id, rate limits, lockout with backoff, no user enumeration |
| T8 | **Supply chain** | Malicious or compromised dependency, model file, or image | Lockfiles with hashes (`uv.lock`, `package-lock.json`), pip-audit, npm audit, Trivy, pinned image digests, models pulled from the official Ollama library with digests recorded |
| T9 | **Data exfiltration through egress** | Compromised component sends data out | Ollama needs no egress after model pull. Sandbox has no network. EC2 security group egress restricted to 443 (GitHub, registries, AWS APIs) |
| T11 | **Telemetry spoofing or poisoning** via a stolen or leaked ingest token | Token copied from a monitored host or app env | Per-integration, write-only tokens (no read capability). Gateway overwrites identity attributes, so a token can only write as *its own* integration. Rotation and revocation. Anomalous-volume alerts. Evidence shows the source integration |
| T12 | **SSRF through health checks** | User-configured URL targets `169.254.169.254`, `localhost`, internal service names, DNS rebinding | Prober in a separate container on an egress-only network. Resolve-then-validate against private, loopback, link-local, CGNAT, and metadata ranges (v4 and v6). IP pinning. Redirect re-validation. IMDSv2 hop limit 1 |
| T13 | **Ingestion DoS or cardinality bombs** | Flood of OTLP or unbounded label values | Caddy body-size limits, per-token rate limits (429), Collector `memory_limiter`, per-integration series caps, integration marked `degraded` when it hits a limit |
| T14 | **Cloud credential misuse** (AWS connector) | Over-broad role, confused deputy | AssumeRole with **ExternalId**, read-only policy published in the CloudFormation template, no static keys, connector code unreachable from LLM modules |
| T15 | **Agent as a host attack vector** | Malicious binary, container socket exposure, remote config abuse | Pinned releases verified by checksum and cosign. Non-root `sentinel-agent` user. GET-only container-socket proxy. **No remote command execution and no remote config push in MVP.** No inbound ports |
| T16 | **Malicious uploads** | Zip bombs, path traversal in archives, huge files, injected instructions | Size and decompression-ratio caps, traversal guard, text-only parsing, redaction, retention limit, same prompt-injection handling as logs |
| T10 | **Path traversal and git abuse** | LLM-requested file paths, crafted repo with symlinks or hooks | Paths resolved and checked to stay under the mirror root. Symlinks not followed. Git hooks disabled (`core.hooksPath=/dev/null`). Mirrors are `--bare` plus worktrees for reading |

## 2. Authentication and authorisation

- **Users:** local accounts, Argon2id (`argon2-cffi`), minimum length 12. Sessions are server-side (`sessions` table) with an opaque random token (256-bit), stored hashed. Idle timeout 12 h, absolute timeout 7 days.
- **Initial admin:** created by a CLI command (`sentinel create-admin`). There is no default credential.
- **RBAC:**

| Permission | viewer | responder | approver | admin |
|---|:-:|:-:|:-:|:-:|
| incidents:read, evidence, reports | ✅ | ✅ | ✅ | ✅ |
| incidents:write (status, comments, feedback, re-investigate, reports) | | ✅ | ✅ | ✅ |
| patches:create | | ✅ | ✅ | ✅ |
| actions:request | | ✅ | ✅ | ✅ |
| actions:approve (low/medium) | | | ✅ | ✅ |
| actions:approve (high/critical; must differ from requester) | | | ✅ | ✅ |
| config:manage (services, repos, credentials) | | | | ✅ |
| integrations:manage (create integrations, tokens, checks, connectors) | | | | ✅ |
| users:manage, apikeys:manage, audit:read | | | | ✅ |

- **Separation of duties:** for risk ≥ high, the approver must differ from the requester. `critical` (purge queue) needs 2 distinct approvers. For the single-user demo, an explicit `DEMO_ALLOW_SELF_APPROVAL=true` setting exists. It is off by default and shown as a banner when on.
- **Machine keys:** scoped, hashed with a server-side pepper, shown once, revocable, `last_used_at` tracked.

## 3. Secrets management

| Secret | Local dev | AWS |
|---|---|---|
| DB passwords, session pepper, HMAC keys, encryption key | `.env` (git-ignored; `.env.example` committed with placeholders) | SSM Parameter Store `SecureString` (KMS), fetched at boot into the container environment by the deploy script |
| GitHub read token / write token | Admin UI → stored AES-GCM encrypted in `credentials` | Same |
| AWS credentials | Not needed | EC2 instance role (no static keys). GitHub Actions deploys through **OIDC federation** |

Rules: secrets never appear in logs (structlog redaction processor), in LLM prompts (the credentials table is unreachable from LLM modules; import-linter contract), or in sandbox containers. `gitleaks` runs in pre-commit and CI.

## 4. Audit logging

Audited events include login success and failure, user, role, and key changes, credential changes, incident status and severity changes, investigation start, hypothesis feedback, action request, approval, rejection, and execution, patch request, PR creation, report generation, and config changes. Each row stores `prev_hash` and `hash = SHA256(prev_hash || canonical_json(row))`. `GET /audit/verify` recomputes the chain. The app DB role has INSERT and SELECT only on `audit_log`.

## 5. Safe tool permissions

| Tool or capability | Caller | Access | Guardrails |
|---|---|---|---|
| Telemetry queries (PromQL, LogQL, Jaeger) | Collectors (code) | Read | Queries built from templates. LLM follow-up requests map to templates, never raw query strings. Result size and time-range caps |
| Code search and read | Code Investigator (via validated requests) | Read, local mirror | Fixed-string search default, path confinement, line caps |
| GitHub REST (read) | `code_intel` | Read token | Rate-limit backoff |
| GitHub write (issue, branch, draft PR) | **Action executor only** | Write token | Only after approval. Branch prefix `sentinel/`. Draft PRs only. Never merge (the token has no admin scope; branch protection on target repos) |
| Sandbox test run | Repair graph → runner | Runner API | Fixed profiles. No command from the LLM |
| Restart or rollback | **Action executor only** → runner | Runner API | Runner-side allowlist plus approval |
| Shell on host | **Nobody** | — | No code path exists. The worker container has no container-engine socket |

## 6. Sandbox isolation (rootless Podman), resource limits, network restrictions

See [Architecture §13](1-system-architecture.md#13-container-execution-sandbox-rootless-podman) for the container settings. In addition:
- The **runner** is the only component with the **rootless Podman API socket** (`$XDG_RUNTIME_DIR/podman/podman.sock` of a dedicated unprivileged user). Even if the runner is compromised, it does not get host root. It runs a minimal FastAPI app with no LLM dependency, has no published ports, and joins only the `sentinel-internal` network.
- **Networks:** `edge` (Caddy ↔ api), `sentinel-internal` (api, worker, scheduler, db, runner, Ollama), `observability` (collector, Loki, Prometheus, Jaeger, plus SENTINEL readers), `demo` (AVIRO). Sandboxes use `none`.
- **EC2 security group:** inbound 80 and 443 only. No 22; admin uses SSM. Prometheus, Loki, Jaeger, and Grafana UIs are not exposed publicly. If needed they sit behind Caddy with authentication.
- **Host:** Podman runs rootless under a dedicated service user (user namespaces by default, no daemon running as root). Automatic security updates (`dnf-automatic` on Amazon Linux 2023). IMDSv2 required, hop limit 1 so containers cannot reach instance credentials.

## 7. Prompt injection: defence in depth

1. **Capability containment.** The model can only produce data: hypotheses, findings, diffs, and narratives. The worst outcome is a misleading suggestion, which is shown next to its evidence.
2. **Input handling.** Strip ANSI and control characters. Truncate long lines. Wrap content in `<evidence>` tags with IDs. The system prompt declares the content untrusted.
3. **Output validation.** Pydantic schemas, citation validation, action catalog plus parameter schema, path allowlists, diff file allowlist (CI configs, Containerfiles, `.github/`, and `.sentinel.yml` are never patchable).
4. **Human gate.** The approval UI shows the exact action, target, parameters, risk, the hypothesis it came from, and a warning if any cited evidence is flagged `suspicious_content`.
5. **Measurement.** Scenario S09 plants injection payloads in logs and code comments. Metrics: an injected instruction appears in proposed actions (must be 0), injected text is adopted as a hypothesis, and false suspicious-content flags.

## 8. Protection against destructive agent actions

- No action type in the catalog can merge, deploy arbitrary images, delete data (apart from the optional, critical-risk DLQ purge), or run commands.
- The executor re-validates at execution time (TOCTOU): the rollback target version still exists in deployment history, the service is still allowlisted, and the approval has not expired (default 30 min).
- Idempotency keys prevent double execution. A kill switch (`ACTIONS_ENABLED=false`) disables all execution globally.

## 9. Web application security

CSP `default-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'`, HSTS, `X-Content-Type-Options`, `Referrer-Policy: same-origin`, SameSite=Strict cookies, CSRF double-submit, CORS disabled (same origin), request body size limits, and Pydantic validation on all inputs.

## 10. Security tests (automated)

| Test | Type |
|---|---|
| Route × role authorisation matrix | API |
| CSRF required on state-changing routes | API |
| Login rate limit and lockout, no user enumeration | API |
| Unapproved, expired, or self-approved (high risk) actions cannot execute | U/I |
| Audit table: UPDATE/DELETE denied for the app role. Chain tamper detected | D |
| Redaction corpus (AWS keys, GitHub tokens, JWTs, passwords in URLs, private keys, emails) → 0 leaks in evidence and prompts | U |
| Sandbox (real rootless Podman): outbound network fails, no container-engine socket inside the sandbox, writes to rootfs fail, fork bomb contained by pids_limit, memory hog OOM-killed, timeout enforced, runs as non-root | I (needs Docker) |
| Runner rejects unsigned, replayed, stale, or non-allowlisted requests | I |
| Path traversal (`../../etc/passwd`, symlink escape) in code tools | U |
| Patch touching `.github/` or `Containerfile` rejected | U |
| XSS payloads in logs render inert (Playwright) | E |
| Prompt-injection scenario S09 metrics | EV |
| Ingest: missing, revoked, or wrong-kind token → 401. Spoofed `sentinel.integration.id` overwritten. Rate limit → 429. Oversized body → 413 | I/SEC |
| Prober SSRF suite: loopback, RFC1918, link-local/metadata, IPv6 ULA, `::ffff:` mapped, decimal or octal IP, DNS rebinding, redirect to private | U/I |
| Agent installer: checksum mismatch aborts. Files `0600`. Runs as non-root. socket proxy denies POST/DELETE | I |
| Upload: zip bomb, nested archive, traversal entry, binary file, oversize → rejected | U |
| AWS connector: wrong ExternalId fails. The policy template contains no write actions (static check) | U |
| gitleaks, pip-audit, npm audit, Trivy image scan, CodeQL (public repo) | CI |
