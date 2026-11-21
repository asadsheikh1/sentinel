# Monitoring a Linux machine with the SENTINEL Agent

The SENTINEL Agent sends a machine's metrics and logs to SENTINEL, and the
metrics and logs of its Podman or Docker containers. It is the standard
OpenTelemetry Collector, pinned to version 0.165.0 and configured by SENTINEL.

It works on any Linux server or VM (EC2, other clouds, on-prem, a local VM)
that can make outbound HTTP(S) connections to SENTINEL's ingest endpoint.

**Supported:**
- **Distributions:** Ubuntu 22.04 and 24.04, Debian 12 and 13, Amazon Linux 2023.
- **CPUs:** x86_64 and arm64.
- **Container mode:** any Linux with Podman or Docker.

## 1. Create the integration

```bash
# Signed in as an admin (cookie + CSRF header). The UI does this for you later.
curl -X POST http://127.0.0.1:8000/api/v1/integrations \
  -H 'Content-Type: application/json' -H "X-CSRF-Token: $CSRF" -b cookies.txt \
  -d '{"name": "web-1", "kind": "container_host", "environment": "prod",
       "expected_signals": ["metrics", "logs"],
       "config": {"watched_processes": ["nginx"]}}'
```

Use `linux_host` for a plain server, or `container_host` to also collect container
stats. The response contains the **ingest token, shown once**. Store it in your
password manager.

`GET /api/v1/integrations/{id}/setup` returns the exact commands for that
integration. The token is never included in them.

## 2. Install

On the machine:

```bash
curl -fsSL https://ingest.example.com/agent/install.sh -o sentinel-agent-install.sh
less sentinel-agent-install.sh          # read it first; it's short
sudo bash sentinel-agent-install.sh --endpoint https://ingest.example.com --integration <id>
```

The installer asks for the token. To pass it without a prompt, use
`--token-file FILE` or the `SENTINEL_INGEST_TOKEN` variable. It never appears on
a command line or in shell history.

The installer:

1. Downloads the agent config. This checks the endpoint, the integration and the token before installing anything.
2. Checks that the agent's loopback ports are free.
3. Downloads otelcol-contrib and verifies it twice. Its SHA-256 must match the value pinned in the script, **and** its Sigstore signature must come from the OpenTelemetry release workflow (checked with a pinned, checksummed cosign). If either check fails, it stops.
4. Creates the system user `sentinel-agent`. It has no login shell and no home directory. The service gives it read access to the journal (`systemd-journal`, `adm`) and nothing else.
5. Writes these files:

   | File | Mode | Contents |
   |---|---|---|
   | `/etc/sentinel-agent/config.yaml` | `0640` | The config. No secrets |
   | `/etc/sentinel-agent/agent.env` | `0600` (root) | Endpoint and token. Only systemd reads it |

6. On container hosts, installs HAProxy as the **GET-only socket proxy** (see below).
7. Validates the config with the real binary, then starts `sentinel-agent.service`. The service is hardened: no capabilities, read-only system, private `/tmp`, and memory and CPU caps.
8. Writes a marker to the journal: `sentinel-verify-<nonce>`.

Within a minute the integration turns `healthy`: metrics and logs are arriving.
You can confirm the full log path with the marker:

```bash
cd backend && uv run sentinel telemetry logs --integration <id> --contains sentinel-verify-<nonce>
```

Re-running the installer is safe. It replaces the config and restarts the agent,
so that's how you apply changed options. The config hash on every series shows
which config an agent is running.

### Container mode

For hosts where you'd rather not install packages, or that have no systemd:

```bash
sudo bash sentinel-agent-install.sh --endpoint ... --integration <id> --mode container
```

This builds the agent image locally from pinned base images. It then runs the
agent with the host mounted read-only at `/hostfs`, next to an HAProxy container
for the socket proxy. Both containers have all capabilities dropped, a read-only
root filesystem and memory limits.

Useful flags:

- `--network host`: lets the agent see the host's network for app OTLP traffic. Its listeners still bind to 127.0.0.1 only.
- `--engine-socket PATH`: a specific engine socket, for example a rootless Podman user's `/run/user/1000/podman/podman.sock`.

## 3. What gets collected

| Data | Where to see it |
|---|---|
| Host metrics | `system_cpu_*`, `system_memory_*`, `system_filesystem_*`, `system_disk_*` and `system_network_*`, labelled with `host_name` |
| Watched processes | `process_*`, labelled with `process_executable_name` and `process_pid` |
| Journal | Loki. Host entries are labelled with the host. Container entries are labelled with the container, as `service_name` |
| Log files | Loki, with `log_file_path` |
| Container stats | `container_*`, labelled with `container_name`. Compose services become `service_name` |
| Agent health | `otelcol_*`: exporter failures, queue size |

Hosts and containers appear under `GET /api/v1/entities`, with a `runs_on`
relation from each container to its host.

## 4. Why a socket proxy

Access to the Podman or Docker API socket means full control of the engine. With
a rootful engine, that is the same as root on the host, even if the socket is
mounted read-only. The agent therefore never gets the socket. It talks to HAProxy,
which:

- allows only `GET` and `HEAD`;
- allows only the endpoints the stats receiver needs: `/_ping`, `/version`, `/events`, `/containers/json`, `/containers/<id>/json` and one-shot `/containers/<id>/stats`;
- answers `403` to everything else: writes, `exec`, `logs`, images, volumes, secrets and system info;
- logs denied requests, so they show up in SENTINEL.

In host mode the proxy runs as the socket's owner with no capabilities, in a
systemd sandbox that can only use Unix sockets.

> **Note:** `/containers/<id>/json` (inspect) includes the container's
> environment variables. The agent reads them only to get names and labels, and
> does not send them. Still, avoid putting secrets in container environment
> variables.

## 5. Options

Set these in the integration's `config`, then re-run the installer. See
[agent/README.md](../../agent/README.md) for the full list.

| Option | Example |
|---|---|
| Watch processes | `"watched_processes": ["nginx", "postgres"]`. Watch daemons, not worker pools: each process gets its own series, and short-lived workers add new ones |
| Application logs | `"log_files": [{"path": "/var/log/app/*.log", "multiline_start": "^\\d{4}-"}]` |
| Only some journal units | `"journald_units": ["nginx.service"]` |
| Scrape a local exporter | `"prometheus_scrapes": [{"job": "celery", "target": "127.0.0.1:9808"}]` |
| Another Collector already uses 4317/4318 | `"otlp_grpc_port": 14317, "otlp_http_port": 14318`, or `"otlp_receiver": false` |

The agent runs as `sentinel-agent`, which can't read log files that only root can
read. Grant read access with a group or an ACL, for example
`setfacl -m u:sentinel-agent:r /var/log/app/app.log`.

## 6. Uninstall

```bash
curl -fsSL https://ingest.example.com/agent/uninstall.sh -o sentinel-agent-uninstall.sh
sudo bash sentinel-agent-uninstall.sh
```

This removes the services, containers and images, files, the user, and HAProxy if
the installer installed it. Data already sent stays in SENTINEL. To make sure
the old token can't be used again, revoke it:
`POST /api/v1/integrations/{id}/tokens/revoke`.

## Footprint

Measured in Step 8 with AVIRO under load (20 synthetic shoppers, 17 containers
watched) on a 4-CPU arm64 VM:

| | CPU (one core) | Private memory | Resident memory |
|---|---|---|---|
| Agent, host mode (Ubuntu 24.04) | 0.5–0.7% | 57 MB | 252 MB |
| Agent, host mode (Amazon Linux 2023) | 0.8% | 57 MB | 250 MB |
| Agent, container mode (Fedora CoreOS) | 1.1% | 76 MB peak | 241 MB |
| Agent, fresh Ubuntu 24.04 VM (2-CPU amd64, CI) | 0.6% | 74 MB | 251 MB |
| GET-only proxy | < 0.1% | 11–17 MB | — |

The resident figure is mostly the 380 MB contrib binary, mapped from disk. That
memory is shared and can be reclaimed. A slimmer agent built with the Collector
Builder (OCB) is planned.

## Troubleshooting

| Symptom | Check |
|---|---|
| `SENTINEL rejected the token` | The token belongs to another integration, or was revoked or rotated |
| `port 4317 is already in use` | Another Collector or Jaeger is running. Change `otlp_grpc_port` / `otlp_http_port` |
| `no Podman/Docker API socket was found` | For rootful Podman: `systemctl enable --now podman.socket`. Or pass `--engine-socket` |
| Integration stays `pending` | `systemctl status sentinel-agent` and `journalctl -u sentinel-agent`. Look for export errors to the endpoint |
| `degraded`: logs missing | Is the journal readable (`journalctl` works as root)? Do the log file permissions allow `sentinel-agent` to read them? |
