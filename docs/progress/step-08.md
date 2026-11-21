# Step 8 report — SENTINEL Agent: Linux hosts and container hosts

**Status:** 🟡 built and tested locally; CI results pending (filled in below once the PR has run).
**PR:** (link added after opening)
**Date:** 2026-11-21

## What was built

| Area | Files |
|---|---|
| Config template | `agent/templates/agent.yaml.j2`. Includes: host metrics (filesystems too), watched processes only, journald, log files with multiline, `docker_stats` through the proxy, optional Prometheus scrapes, a loopback OTLP receiver for local apps, the agent's own metrics, and a persistent send queue. Config hash and agent version are added to every resource |
| Renderer | `sentinel/integrations/agent_config.py`. Strict option validation (also applied on integration create and update). Every value goes in through `tojson` in a sandboxed Jinja environment, and the output is re-parsed and checked |
| API | `GET /api/v1/integrations/{id}/agent-config` (new **ingest-token** access rule: only that integration's live token), `GET /api/v1/integrations/{id}/setup` (install commands, never the token), `GET /agent/{install.sh,uninstall.sh,socket-proxy.cfg,Containerfile}` (public, allowlisted) |
| Ingest endpoint | Caddy also serves the agent files and the per-integration config, so `:4319` (`ingest.<domain>` on AWS) is the only SENTINEL address a host needs |
| Installer | `agent/install.sh` (see below) |
| Uninstaller | `agent/uninstall.sh`: services, containers, images it pulled, files, user, and HAProxy if the installer added it |
| Socket proxy | `agent/socket-proxy.cfg` (HAProxy): GET/HEAD only, six read endpoints, one-shot stats only, denials logged |
| Container variant | `agent/Containerfile` (pinned Collector + `journalctl`), `install.sh --mode container` |
| Prometheus | promotes `sentinel.agent.config_hash`, `process.executable.name` and `process.pid` |
| Tests | `tests/unit/test_agent_config.py`, `tests/db/test_agent_api.py`, `tests/agent/test_agent_e2e.py`, `tests/agent/render_matrix.py`, `tests/agent/Containerfile.systemd`, gateway test for the new Caddy routes |
| CI | `.github/workflows/ci-agent.yml`: render + `otelcol validate` every config, shellcheck, HAProxy config check, image build; install in Ubuntu 24.04 and Amazon Linux 2023 systemd machines; install directly on the runner's fresh Ubuntu 24.04 VM |
| Docs | [Monitoring a Linux machine](../guides/agent.md), [agent/README.md](../../agent/README.md), `docs/rbac.md` regenerated |

### What the installer does

1. Fetches the config first. This proves the endpoint, integration and token before anything is installed.
2. Checks that the agent's loopback ports are free.
3. Downloads otelcol-contrib 0.165.0 and checks it against a **pinned SHA-256**. It also checks the **Sigstore signature**, using a pinned and checksummed cosign 3.1.4, against the OpenTelemetry release workflow identity.
4. Creates `sentinel-agent`.
5. Writes `config.yaml` (`0640`) and `agent.env` (`0600`, root). The token is only ever in `agent.env`, never on a command line.
6. Puts HAProxy in front of the engine socket.
7. Validates with the real binary and starts a hardened systemd unit: no capabilities, `NoNewPrivileges`, `ProtectSystem=strict`, memory and CPU caps.
8. Writes `sentinel-verify-<nonce>` to the journal.

## Acceptance criteria (roadmap)

| Criterion | Result |
|---|---|
| On a fresh Ubuntu VM the installer brings the integration to `healthy` within 2 min | ✅ locally in an Ubuntu 24.04 systemd machine: **healthy 23–25 s** after starting the installer. ⏳ the runner-VM install in CI (below) |
| Host metrics queryable and attributed | ✅ CPU, memory, disk and network, labelled with the host and the integration. Filesystems: ✅ in container mode via `/hostfs`; in a test container the scraper skips them (see limitations); ⏳ on the CI VM |
| Journal queryable | ✅ the verification marker arrives through journald → gateway → Loki |
| Container metrics and logs queryable and attributed | ✅ metrics for all 17 containers (AVIRO + SENTINEL), labelled with the container and the host. Logs: ✅ AVIRO's container logs in container mode, attributed to each container (`service_name=aviro_postgres_1`, …). ⏳ host mode in CI |
| Footprint numbers published | ✅ below and in the guide |

### Roadmap test list

| Test | Result |
|---|---|
| Config rendering unit tests | ✅ 51 tests: structure, options, loopback-only listeners, hash, injection attempts, a Hypothesis property over 150 generated paths and process names, filesystem exclusions, broken-template detection |
| `otelcol validate` on every rendered config | ✅ 12 of 12 configs locally (2 kinds × 2 modes × 3 option sets). A deliberately broken OTTL statement is rejected, so the check is real. ⏳ CI |
| Install test in a disposable Ubuntu 24.04 systemd container | ✅ 14 passed, 1 skipped (container logs can't be seen from inside the test container; covered by container mode) |
| Install test in a Lima VM | ❌ not done. Lima isn't installed; I said I'd only add it with your OK. Replaced by installing directly on GitHub's fresh Ubuntu 24.04 VM in CI (⏳) |
| Verification marker received end to end | ✅ |
| Uninstall leaves no residue | ✅ host mode: no files, units, user, group, processes, or HAProxy package. Container mode: no containers, volumes, images, files or user |
| Footprint under AVIRO load | ✅ below |
| Socket proxy denies POST | ✅ POST, PUT and DELETE (also to allowed paths), `logs`, streaming `stats`, `/info`, `/images`, `/secrets` and path tricks all get 403. The agent user can't connect to the raw socket |
| Amazon Linux 2023 | ✅ 13 passed, 1 skipped, 1 failed locally: the 2-minute check. The install took 141 s because `dnf` was slow over this network; the agent was healthy 6 s after it started. ⏳ CI |

### Footprint (AVIRO under load: 20 synthetic shoppers, 17 containers watched, 4-CPU arm64)

| | CPU (one core, 60 s) | Private memory (RssAnon) | Resident (VmRSS) |
|---|---|---|---|
| Agent, host mode, Ubuntu 24.04 | 0.47–0.73% | 57 MB | 252 MB |
| Agent, host mode, Amazon Linux 2023 | 0.83% | 57 MB | 250 MB |
| Agent, container mode, Fedora CoreOS (Podman VM) | 1.08% | 76 MB peak | 241 MB |
| GET-only proxy (HAProxy) | < 0.1% | 11–17 MB | — |

The design estimate was "50–150 MB RAM, < 2% CPU". Private memory and CPU are
within it. Resident memory is higher: about 195 MB of it is the 380 MB contrib
binary mapped from disk, which is shared and reclaimable. The planned OCB slim
build would shrink it.

### Test totals (local)

| Suite | Result |
|---|---|
| Backend unit + database + integration (`make test-all`) | 297 passed, coverage 94.4% |
| Live gateway (`make test-gateway`) | 16 passed (one new: only the agent files and the token's own config are exposed on `:4319`) |
| Agent end to end (`make test-agent`), Ubuntu 24.04, under AVIRO load | 14 passed, 1 skipped |
| Agent end to end, Amazon Linux 2023 | 13 passed, 1 skipped, 1 failed (slow local `dnf`, above) |
| Container mode in the Podman VM | install, data, uninstall checked by hand (commands below) |

### Checking that the tests can actually fail
I removed the proxy's "GET/HEAD only" rule. **The proxy test still passed.** Every write it tried also hit a path outside the allowlist, so the second rule blocked it anyway. I added writes to *allowed* paths (`POST /containers/json`, `DELETE /_ping`, `PUT /containers/x/json`). With the rule removed, the test then failed on `POST /v1.41/containers/json`. With the rule restored, it passes.

`otelcol validate` rejected a deliberately broken OTTL statement, and the unit tests catch a template that references an undefined processor or adds an exporter.

## Bugs found and fixed during this step

1. **The endpoint check rejected every URL.** It was a bracket-expression error in a bash regex (`[...\[\]-]`).
2. **The installer's validate step crashed** (`panic: stat .: permission denied`). It ran as `sentinel-agent` from `/root`, and the Collector reads its working directory at start-up. It now runs from `/`.
3. **Deprecated component names.** In 0.165, `hostmetrics`, `resourcedetection` and `filelog` are aliases for `host_metrics`, `resource_detection` and `file_log`. The new names are used, and no deprecation warnings remain.
4. **Filesystem exclusions were unanchored regexes.** `"/dev/*"` would also have hidden `/data/devices`. Container layers were excluded by fs type (`overlay`), which would hide a host whose root is on overlay. The patterns are now anchored and exclude container layers by path, rootless Podman storage included. Covered by 11 unit cases.
5. **Port clash.** With host networking the agent's 4317/4318 clashed with another listener, and the agent crash-looped while the installer reported "running". Fixes:
   - The ports are now options.
   - The installer refuses up front when a port is taken, naming the process holding it.
   - Container mode now requires the container to stay up, with no restarts, for 10 s.
6. **Container mode couldn't reach the proxy**, for three separate reasons:
   - The image's user had a different group ID from the proxy socket's group.
   - The proxy ran as uid 0, which can't open a rootless user's socket; it now runs as the socket's owner.
   - A stale socket in a root-owned volume blocked restarts; the containers now share a host directory with the right owner.
7. **Proxy logging.** The proxy now logs only denied requests: an audit trail of blocked engine calls, without a stream of allowed polls fed back through the journal. (Before, nothing was logged at all: the log level was above the request level.)
8. **Uninstall left about 500 MB of images** in container mode: the pulled base images and Podman build-cache layers. The installer now records which images it pulled, builds without cached layers, and the uninstaller removes exactly those.
9. **The test helper printed the ingest token** in a failure message (dataclass repr). The token and the installer output are now hidden from reprs.

## Decisions worth reviewing

1. **API image build context is now the repository root**, so the image can carry `agent/`. A root `.containerignore` sends only `backend/` and `agent/`. Updated: `compose.yaml`, `Makefile`, `ci-backend.yml`. The dev compose also mounts `agent/` read-only into the API container, so installer edits need no rebuild.
2. **Agent routes on the ingest endpoint.** Caddy proxies `GET /agent/<4 files>` and `GET /api/v1/integrations/<uuid>/agent-config`, and nothing else of the API. This keeps a single outbound destination for monitored hosts (the integration design's "outbound HTTPS only").
3. **New access rule `ingest_token()`.** It is in `docs/rbac.md`, and the route-matrix test treats it like API-key routes: a user session never satisfies it.
4. **Collector tarball instead of the `.deb`/`.rpm`.** The packages' install scripts start their own service with a default config that listens on all interfaces. The tarball is the same signed release, carries no scripts, and gives one code path for every distribution. The distribution check only decides how to install prerequisites (curl, HAProxy).
5. **HAProxy as the socket proxy.** It comes from the distribution's own repos on hosts (no extra download to trust), and is a pinned official image in container mode.
6. **The OTLP receiver binds to loopback even in container mode.** Apps reach it from the same network namespace (or `--network container:sentinel-agent`), so the agent never listens on a network.
7. **`process.pid` is promoted in Prometheus.** Without it, two processes with the same name would collide. The cost is new series when processes restart, so the guide says to watch daemons, not worker pools. Watching `postgres` produced about 1,400 series in 5 minutes because of short-lived backends.

## Known limitations

- **Lima VM test not run** (above). CI's fresh Ubuntu VM covers the "fresh VM" criterion instead.
- **In a test container, filesystem metrics are skipped.** The scraper reports physical filesystems only, and skips a disk whose device node isn't present. Every disk inside a rootless container is like that. On VMs, and in container mode via `/hostfs`, they're reported.
- **Docker's default `json-file` container logs are not collected.** Container logs come from journald, which is the Podman default on systemd hosts and an option for Docker (`log-driver: journald`). Container *stats* work with both engines.
- **The installer needs GitHub** (Collector and cosign downloads) and Docker Hub (container mode). `--package-file` and `--cosign-file` support pre-downloaded copies, which are still verified.
- **No "config out of date" view yet.** The hash is reported on every series; the UI comparison comes with the frontend steps.
- **The `/containers/<id>/json` endpoint (inspect) returns container environment variables** to the agent, which needs it for names and labels. This is documented in the guide.

## How you verify

```bash
cd "/Users/asadsheikh/Coding/SENTINEL Autonomous AI Site Reliability Engineer"
git checkout step-08-agent
make stack-up-fast
make test-agent                    # Ubuntu 24.04 systemd machine: install → data → proxy → uninstall
# faster, with pre-downloaded (still verified) files:
#   SENTINEL_AGENT_PACKAGE_DIR=/path/with/tarball+sigstore+cosign make test-agent
cd backend && uv run pytest -q tests/unit/test_agent_config.py && cd ..
```

By hand, against the setup endpoint (as an admin):
`GET /api/v1/integrations/{id}/setup` → run its `install_command` on any supported Linux machine.

## Not in this step (by design)

- Alert rules for host signals (Step 10).
- The integration setup UI and its live checklist (frontend steps).
- OpAMP remote management: not in the MVP (ADR-0016).
- The OCB slim build.
- The PostgreSQL receiver (Should).
