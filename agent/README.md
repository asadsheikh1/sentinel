# SENTINEL Agent

The SENTINEL Agent is a pinned release of the OpenTelemetry Collector (contrib),
configured by SENTINEL ([ADR-0013](../docs/adr/0013-otel-collector-as-agent.md)).
It sends a Linux machine's metrics and logs, and its Podman or Docker containers'
metrics and logs, to SENTINEL. It only sends data
([ADR-0016](../docs/adr/0016-data-only-agent.md)): no inbound ports, no remote
commands, no remote configuration.

How to install it: [docs/guides/agent.md](../docs/guides/agent.md).

## Files

| File | What it is |
|---|---|
| `templates/agent.yaml.j2` | The Collector config template. SENTINEL renders it per integration (`backend/src/sentinel/integrations/agent_config.py`) and serves it at `GET /api/v1/integrations/{id}/agent-config` (ingest-token auth) |
| `install.sh` | Installer. Verifies the download (pinned SHA-256 **and** Sigstore signature), creates the `sentinel-agent` user, writes the config and a `0600` env file, starts a systemd service (or, with `--mode container`, two containers), writes a verification marker to the journal |
| `uninstall.sh` | Removes everything the installer created |
| `socket-proxy.cfg` | HAProxy config for the GET-only container-engine proxy |
| `Containerfile` | Agent image for container mode: the pinned Collector plus `journalctl` |

SENTINEL serves `install.sh`, `uninstall.sh`, `socket-proxy.cfg` and `Containerfile`
at `/agent/<name>` on the ingest endpoint. That is the only SENTINEL address a
monitored machine needs.

## What it collects

| Source | Receiver | Notes |
|---|---|---|
| CPU, load, memory, paging, disk I/O, network, filesystems, process counts | `host_metrics` | Pseudo and container-layer filesystems excluded |
| Watched processes | `host_metrics/processes` | Only the executable names listed in `watched_processes` |
| systemd journal | `journald` | Optionally limited to `journald_units`. Container log lines (journald log driver) become the container's resource |
| Log files | `file_log/N` | One per `log_files` entry, optional multiline start pattern, offsets kept across restarts |
| Container stats | `docker_stats` | Through the GET-only proxy. Compose labels become `service.name` / `service.namespace` |
| Prometheus exporters | `prometheus/scrape` | `prometheus_scrapes` entries (e.g. Celery or Postgres exporters) |
| Local apps | `otlp` | `127.0.0.1:4317` (gRPC) and `:4318` (HTTP), configurable |
| The agent itself | `prometheus/self` | Exporter failures, queue size, dropped data (`127.0.0.1:18888`) |

Every resource carries `sentinel.agent.config_hash` and `sentinel.agent.version`.
The SENTINEL gateway adds the integration identity from the token.

## Options (`integrations.config`)

```json
{
  "collection_interval_s": 30,
  "watched_processes": ["nginx", "postgres"],
  "journald": true,
  "journald_units": [],
  "log_files": [{"path": "/var/log/app/*.log", "multiline_start": "^\\d{4}-\\d{2}-\\d{2}"}],
  "containers": null,
  "container_engine": "podman",
  "prometheus_scrapes": [{"job": "celery", "target": "127.0.0.1:9808", "path": "/metrics", "interval_s": 30}],
  "otlp_receiver": true,
  "otlp_grpc_port": 4317,
  "otlp_http_port": 4318
}
```

`containers: null` means on for `container_host` and off for `linux_host`. The
options are validated on create and update. Unknown keys, relative paths, `..`,
quotes, spaces and other unexpected characters are rejected.

## Updating the pinned versions

`install.sh` pins the Collector version with the SHA-256 of both tarballs, the
cosign version with its two SHA-256s, and the HAProxy image digest. When you bump
the Collector, also update `AGENT_VERSION` in `agent_config.py` and the image in
`deploy/compose/compose.observability.yaml`. CI then validates every rendered
config with the new binary.
