#!/usr/bin/env bash
# SENTINEL Agent installer: a pinned OpenTelemetry Collector (contrib) that sends
# this machine's metrics and logs (and its containers') to SENTINEL.
#
#   curl -fsSL --retry 5 <endpoint>/agent/install.sh -o sentinel-agent-install.sh
#   sudo bash sentinel-agent-install.sh --endpoint <endpoint> --integration <id>
#
# The ingest token is read from a prompt, from --token-file, or from the
# SENTINEL_INGEST_TOKEN environment variable, so it never lands in shell history.
#
# What it does (and nothing else):
#   * downloads otelcol-contrib, checks its SHA-256 against the value pinned
#     below AND its Sigstore (cosign) signature from the OpenTelemetry release
#     workflow, and refuses to continue if either fails;
#   * creates the non-root user `sentinel-agent` (journal and log read access only);
#   * writes /etc/sentinel-agent/{config.yaml,agent.env} (the env file is 0600);
#   * installs and starts sentinel-agent.service (systemd), or, with
#     --mode container, the agent container;
#   * for container hosts, puts a GET-only socket proxy in front of the Podman or
#     Docker API, so the agent never holds the engine socket;
#   * writes a verification marker to the journal.
#
# The agent only sends data: no inbound ports, no remote commands (ADR-0016).
# Supported: Ubuntu 22.04/24.04, Debian 12/13, Amazon Linux 2023; x86_64 and arm64.
# Re-running it is safe: it updates the config and restarts the agent.

set -Eeuo pipefail
umask 077

# ---------------------------------------------------------------- pinned versions
OTELCOL_VERSION="0.165.0"
declare -rA OTELCOL_SHA256=(
  [amd64]="bd6e73d94bf3359ad6fac25529a8ee308adb9b0b8a0e8af034ed3891fbe5aa33"
  [arm64]="376420729c862230807bc89811d07d10031eaeca3f6481fe371a2bdcefd2c8fa"
)
OTELCOL_SIGNER="https://github.com/open-telemetry/opentelemetry-collector-releases/.github/workflows/base-release.yaml@refs/tags/v${OTELCOL_VERSION}"
COSIGN_VERSION="3.1.4"
declare -rA COSIGN_SHA256=(
  [amd64]="424e2c97d0b45fd7a6b608a87f090f35633f4c23ece5f092d48bba11aaf4bbfa"
  [arm64]="ade83b7aac62e131cacfe96d958948b2150cc9efec46a570a2649b8e27dedad2"
)
OIDC_ISSUER="https://token.actions.githubusercontent.com"
PROXY_IMAGE="docker.io/library/haproxy:3.2.27-alpine@sha256:7816a9801a6846b912bc37e48529808e2fa87c3c47992441940f68e393ebcfeb"
AGENT_IMAGE="localhost/sentinel-agent:${OTELCOL_VERSION}"

# ---------------------------------------------------------------- layout
PREFIX=/opt/sentinel-agent
ETC=/etc/sentinel-agent
STATE_DIR=/var/lib/sentinel-agent
UNIT=/etc/systemd/system/sentinel-agent.service
PROXY_UNIT=/etc/systemd/system/sentinel-agent-socket-proxy.service
PROXY_SOCKET=/run/sentinel-agent-proxy/engine.sock
AGENT_USER=sentinel-agent
SELF_METRICS=http://127.0.0.1:18888/metrics

ENDPOINT="" INTEGRATION="" MODE=host TOKEN_FILE="" ENGINE_SOCKET="" PACKAGE_FILE="" COSIGN_FILE=""
NETWORK="" WORK=""

log() { printf '\033[1;34m==>\033[0m [%3ss] %s\n' "$SECONDS" "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
cleanup() { [[ -n "$WORK" && -d "$WORK" ]] && rm -rf "$WORK"; return 0; }
trap cleanup EXIT
trap 'die "failed at line $LINENO; fix the cause and re-run (re-running is safe)"' ERR

usage() {
  cat <<'EOF'
Usage: install.sh --endpoint URL --integration ID [options]

  --endpoint URL        SENTINEL ingest endpoint, e.g. https://ingest.example.com
  --integration ID      the integration's ID (from SENTINEL's setup page)
  --mode host|container systemd service (default) or a Podman/Docker container
  --token-file FILE     read the ingest token from FILE (default: prompt)
  --engine-socket PATH  container engine API socket (default: auto-detect)
  --package-file FILE   use a downloaded otelcol-contrib tarball (still verified;
                        FILE.sigstore.json must sit next to it)
  --cosign-file FILE    use a downloaded copy of the pinned cosign (still verified)
  --network NAME        container mode: network for the agent container
  -h, --help            this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --endpoint) ENDPOINT="${2:-}"; shift 2 ;;
    --integration) INTEGRATION="${2:-}"; shift 2 ;;
    --mode) MODE="${2:-}"; shift 2 ;;
    --token-file) TOKEN_FILE="${2:-}"; shift 2 ;;
    --engine-socket) ENGINE_SOCKET="${2:-}"; shift 2 ;;
    --package-file) PACKAGE_FILE="${2:-}"; shift 2 ;;
    --cosign-file) COSIGN_FILE="${2:-}"; shift 2 ;;
    --network) NETWORK="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done

# ---------------------------------------------------------------- checks
[[ $EUID -eq 0 ]] || die "run as root (sudo bash $0 ...)"
endpoint_re='^https?://[][A-Za-z0-9.:-]+$'
[[ "$ENDPOINT" =~ $endpoint_re ]] || die "--endpoint must be like https://ingest.example.com (no path)"
[[ "$INTEGRATION" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || die "--integration must be the integration's UUID"
[[ "$MODE" == host || "$MODE" == container ]] || die "--mode must be host or container"
[[ "$ENDPOINT" == http://* && ! "$ENDPOINT" =~ ^http://(127\.0\.0\.1|localhost|\[::1\]|ingest\.sentinel\.internal)(:|$) ]] \
  && warn "the endpoint is plain http: the token and data are not encrypted in transit"
[[ -z "$NETWORK" || "$NETWORK" =~ ^[A-Za-z0-9_.-]{1,64}$ ]] || die "--network: invalid name"

case "$(uname -m)" in
  x86_64|amd64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) die "unsupported CPU architecture: $(uname -m) (x86_64 and arm64 are supported)" ;;
esac

[[ -r /etc/os-release ]] || die "cannot identify the distribution (/etc/os-release missing)"
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}:${VERSION_ID:-}" in
  ubuntu:22.04|ubuntu:24.04|debian:12|debian:13) PKG=apt ;;
  amzn:2023) PKG=dnf ;;
  *)
    # Container mode only needs Podman or Docker, so any distribution will do.
    [[ "$MODE" == container ]] \
      || die "unsupported distribution: ${PRETTY_NAME:-unknown} (supported: Ubuntu 22.04/24.04, Debian 12/13, Amazon Linux 2023; elsewhere use --mode container)"
    if command -v apt-get >/dev/null; then PKG=apt; elif command -v dnf >/dev/null; then PKG=dnf; else PKG=none; fi
    ;;
esac

if [[ "$MODE" == host ]]; then
  [[ -d /run/systemd/system ]] || die "systemd is not running; use --mode container on hosts without systemd"
fi

pkg_install() {
  [[ "$PKG" != none ]] || die "missing: $* (install it and re-run)"
  if [[ "$PKG" == apt ]]; then
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$@" >/dev/null
  else
    dnf install -y -q "$@" >/dev/null
  fi
}

need=()
command -v curl >/dev/null || need+=(curl)
command -v tar >/dev/null || need+=(tar)
command -v gzip >/dev/null || need+=(gzip)
command -v logger >/dev/null || { [[ "$PKG" == apt ]] && need+=(bsdutils) || need+=(util-linux); }
command -v sha256sum >/dev/null || need+=(coreutils)
[[ -e /etc/ssl/certs/ca-certificates.crt || -e /etc/pki/tls/certs/ca-bundle.crt ]] || need+=(ca-certificates)
if [[ ${#need[@]} -gt 0 ]]; then
  log "Installing prerequisites: ${need[*]}"
  pkg_install "${need[@]}"
  log "Prerequisites installed"
fi

WORK="$(mktemp -d)"
chmod 0700 "$WORK"

# ---------------------------------------------------------------- token
TOKEN="${SENTINEL_INGEST_TOKEN:-}"
if [[ -n "$TOKEN_FILE" ]]; then
  [[ -r "$TOKEN_FILE" ]] || die "cannot read --token-file $TOKEN_FILE"
  TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
elif [[ -z "$TOKEN" ]]; then
  [[ -r /dev/tty ]] || die "no terminal: pass --token-file or set SENTINEL_INGEST_TOKEN"
  read -r -s -p "SENTINEL ingest token: " TOKEN < /dev/tty
  echo >&2
fi
[[ "$TOKEN" =~ ^sit_[a-z0-9]{8}_[A-Za-z0-9_-]{43}$ ]] || die "that is not a SENTINEL ingest token (sit_...)"

# The token goes to curl through a file, never on a command line (ps would show it).
printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" > "$WORK/auth.curlrc"

# ---------------------------------------------------------------- config first
# Fetching the config proves the endpoint, the integration and the token before
# anything is installed.
log "Downloading the agent config for integration $INTEGRATION"
status="$(curl -sS --retry 5 -o "$WORK/config.yaml" -D "$WORK/headers" -w '%{http_code}' \
  --proto '=https,http' --max-time 30 -K "$WORK/auth.curlrc" \
  "$ENDPOINT/api/v1/integrations/$INTEGRATION/agent-config?mode=$MODE")" \
  || die "cannot reach $ENDPOINT"
case "$status" in
  200) ;;
  401) die "SENTINEL rejected the token (wrong token, revoked, or not this integration's)" ;;
  404) die "integration $INTEGRATION not found, or it is not a host/container integration" ;;
  429) die "rate limited by SENTINEL; try again in a minute" ;;
  *) die "unexpected HTTP $status from SENTINEL" ;;
esac
CONFIG_HASH="$(awk -F': ' 'tolower($1)=="x-sentinel-config-hash"{gsub(/\r/,"",$2); print $2}' "$WORK/headers")"
[[ "$CONFIG_HASH" =~ ^[0-9a-f]{16}$ ]] || die "the config response has no valid config hash"
grep -q '^  docker_stats:' "$WORK/config.yaml" && CONTAINERS=1 || CONTAINERS=0

# The agent's loopback listeners (local OTLP, its own metrics) must be free, or
# it would start and then fail. Ports held by an earlier SENTINEL Agent are fine.
if [[ "$MODE" == host || "$NETWORK" == host ]] && command -v ss >/dev/null; then
  # Listeners only: the OTLP receiver endpoints and the self-metrics port (not
  # scrape targets, which are other programs' ports and meant to be in use).
  { grep -oE 'endpoint: "127\.0\.0\.1:[0-9]+"' "$WORK/config.yaml" | grep -oE '[0-9]+"$' | tr -d '"'
    grep -oE '^ +port: [0-9]+$' "$WORK/config.yaml" | grep -oE '[0-9]+$'
  } | sort -u > "$WORK/ports"
  while read -r port; do
    holder="$(ss -Hltnp "sport = :$port" 2>/dev/null | grep -v '"otelcol-contrib"' || true)"
    [[ -z "$holder" ]] || die "port $port is already in use on this machine ($(echo "$holder" | head -1 | awk '{print $NF}')). Set otlp_grpc_port / otlp_http_port (or otlp_receiver: false) in the integration's agent options, then re-run."
  done < "$WORK/ports"
fi

# ---------------------------------------------------------------- engine socket
if [[ "$CONTAINERS" == 1 ]]; then
  if [[ -z "$ENGINE_SOCKET" ]]; then
    if [[ -S /run/podman/podman.sock ]]; then
      ENGINE_SOCKET=/run/podman/podman.sock
    elif command -v podman >/dev/null && [[ -d /run/systemd/system ]]; then
      log "Enabling podman.socket (the Podman API, used read-only through the proxy)"
      systemctl enable --now podman.socket >/dev/null 2>&1 || true
      [[ -S /run/podman/podman.sock ]] && ENGINE_SOCKET=/run/podman/podman.sock
    fi
    [[ -z "$ENGINE_SOCKET" && -S /var/run/docker.sock ]] && ENGINE_SOCKET=/var/run/docker.sock
  fi
  [[ -n "$ENGINE_SOCKET" && -S "$ENGINE_SOCKET" ]] \
    || die "container collection is on, but no Podman/Docker API socket was found (use --engine-socket)"
  log "Container engine socket: $ENGINE_SOCKET (the agent reaches it only through the GET-only proxy)"
fi

# ---------------------------------------------------------------- user
nologin="$(command -v nologin || echo /usr/sbin/nologin)"
if ! id -u "$AGENT_USER" >/dev/null 2>&1; then
  log "Creating system user $AGENT_USER"
  useradd --system --user-group --no-create-home --home-dir /nonexistent --shell "$nologin" "$AGENT_USER"
fi
GROUPS_LIST=()
for g in systemd-journal adm; do getent group "$g" >/dev/null && GROUPS_LIST+=("$g"); done

install -d -m 0755 "$PREFIX" "$PREFIX/bin"
install -d -m 0750 -o root -g "$AGENT_USER" "$ETC"
install -d -m 0750 -o "$AGENT_USER" -g "$AGENT_USER" "$STATE_DIR"
STATE="$PREFIX/state"
touch "$STATE"
state_set() { grep -v "^$1=" "$STATE" > "$STATE.new" || true; echo "$1=$2" >> "$STATE.new"; mv "$STATE.new" "$STATE"; }

# ---------------------------------------------------------------- collector binary
verify_collector() {
  local pkg="$1" bundle="$2" want="${OTELCOL_SHA256[$ARCH]}" got
  got="$(sha256sum "$pkg" | cut -d' ' -f1)"
  [[ "$got" == "$want" ]] || die "checksum mismatch for $(basename "$pkg"): got $got, pinned $want. Not installing."
  log "Checksum OK ($want)"

  if [[ -n "$COSIGN_FILE" ]]; then
    cp "$COSIGN_FILE" "$WORK/cosign"
  else
    curl -fsSL --proto '=https' --retry 3 -o "$WORK/cosign" \
      "https://github.com/sigstore/cosign/releases/download/v${COSIGN_VERSION}/cosign-linux-${ARCH}"
  fi
  [[ "$(sha256sum "$WORK/cosign" | cut -d' ' -f1)" == "${COSIGN_SHA256[$ARCH]}" ]] \
    || die "checksum mismatch for cosign ${COSIGN_VERSION}. Not installing."
  chmod 0700 "$WORK/cosign"
  HOME="$WORK" "$WORK/cosign" verify-blob "$pkg" --bundle "$bundle" \
    --certificate-identity "$OTELCOL_SIGNER" --certificate-oidc-issuer "$OIDC_ISSUER" >/dev/null 2>"$WORK/cosign.log" \
    || { cat "$WORK/cosign.log" >&2; die "signature verification failed. Not installing."; }
  log "Signature OK (signed by the OpenTelemetry release workflow, v${OTELCOL_VERSION})"
}

if [[ "$MODE" == host ]]; then
  tarball="otelcol-contrib_${OTELCOL_VERSION}_linux_${ARCH}.tar.gz"
  if [[ -n "$PACKAGE_FILE" ]]; then
    [[ -r "$PACKAGE_FILE" && -r "$PACKAGE_FILE.sigstore.json" ]] \
      || die "--package-file needs FILE and FILE.sigstore.json"
    cp "$PACKAGE_FILE" "$WORK/$tarball"
    cp "$PACKAGE_FILE.sigstore.json" "$WORK/$tarball.sigstore.json"
  else
    base="https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${OTELCOL_VERSION}"
    log "Downloading otelcol-contrib ${OTELCOL_VERSION} (${ARCH}, ~110 MB)"
    curl -fsSL --proto '=https' --retry 5 -C - -o "$WORK/$tarball" "$base/$tarball"
    curl -fsSL --proto '=https' --retry 3 -o "$WORK/$tarball.sigstore.json" "$base/$tarball.sigstore.json"
  fi
  verify_collector "$WORK/$tarball" "$WORK/$tarball.sigstore.json"
  tar -xzf "$WORK/$tarball" -C "$WORK" otelcol-contrib
  install -m 0755 -o root -g root "$WORK/otelcol-contrib" "$PREFIX/bin/otelcol-contrib.new"
  mv -f "$PREFIX/bin/otelcol-contrib.new" "$PREFIX/bin/otelcol-contrib"
fi

# ---------------------------------------------------------------- config files
# Config: no secrets (endpoint and token come from the env file).
install -m 0640 -o root -g "$AGENT_USER" "$WORK/config.yaml" "$ETC/config.yaml"
# Env file: the only place the token is stored. Read by systemd (root) only.
{
  echo "SENTINEL_INGEST_ENDPOINT=$ENDPOINT"
  echo "SENTINEL_INGEST_TOKEN=$TOKEN"
} > "$WORK/agent.env"
install -m 0600 -o root -g root "$WORK/agent.env" "$ETC/agent.env"
state_set mode "$MODE"
state_set integration "$INTEGRATION"
state_set config_hash "$CONFIG_HASH"

# ---------------------------------------------------------------- socket proxy
if [[ "$CONTAINERS" == 1 ]]; then
  curl -fsS --retry 5 --proto '=https,http' --max-time 30 -o "$WORK/socket-proxy.cfg" "$ENDPOINT/agent/socket-proxy.cfg" \
    || die "cannot download the socket proxy config"
  grep -q 'deny_status 403 unless METH_GET' "$WORK/socket-proxy.cfg" || die "unexpected socket proxy config"
  install -m 0644 -o root -g root "$WORK/socket-proxy.cfg" "$ETC/socket-proxy.cfg"
  state_set engine_socket "$ENGINE_SOCKET"
fi

if [[ "$MODE" == host && "$CONTAINERS" == 1 ]]; then
  if ! command -v haproxy >/dev/null; then
    log "Installing HAProxy (the GET-only socket proxy)"
    pkg_install haproxy
    state_set installed_haproxy 1
    # The distribution's own haproxy service is not used.
    systemctl disable --now haproxy.service >/dev/null 2>&1 || true
  fi
  # The proxy runs as the socket's owner (it needs that access anyway), with no
  # capabilities, in a read-only sandbox that can only use Unix sockets.
  socket_owner="$(stat -c %U "$ENGINE_SOCKET")"
  socket_dir="$(dirname "$ENGINE_SOCKET")"
  # Rootless engines keep their socket under /run/user, which ProtectHome=yes hides.
  protect_home=yes
  [[ "$ENGINE_SOCKET" == /run/user/* ]] && protect_home=read-only
  cat > "$PROXY_UNIT" <<EOF
# Installed by the SENTINEL Agent installer. GET-only proxy for the container engine API.
[Unit]
Description=SENTINEL Agent container-engine socket proxy (GET only)
After=network.target

[Service]
User=$socket_owner
Group=$AGENT_USER
Environment=SENTINEL_ENGINE_SOCKET=$ENGINE_SOCKET
Environment=SENTINEL_PROXY_SOCKET=$PROXY_SOCKET
ExecStartPre=$(command -v haproxy) -c -q -f $ETC/socket-proxy.cfg
ExecStart=$(command -v haproxy) -db -f $ETC/socket-proxy.cfg
RuntimeDirectory=sentinel-agent-proxy
RuntimeDirectoryMode=0750
Restart=on-failure
RestartSec=5
CapabilityBoundingSet=
AmbientCapabilities=
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=$protect_home
PrivateTmp=yes
PrivateDevices=yes
PrivateNetwork=yes
ReadOnlyPaths=/
ReadWritePaths=$socket_dir
RestrictAddressFamilies=AF_UNIX
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
LockPersonality=yes
SystemCallArchitectures=native
MemoryMax=64M

[Install]
WantedBy=multi-user.target
EOF
fi

# ---------------------------------------------------------------- host mode
start_host() {
  local wants="" groups="${GROUPS_LIST[*]}"
  [[ "$CONTAINERS" == 1 ]] && wants="sentinel-agent-socket-proxy.service"
  cat > "$UNIT" <<EOF
# Installed by the SENTINEL Agent installer. Data out only: no inbound ports.
[Unit]
Description=SENTINEL Agent (OpenTelemetry Collector ${OTELCOL_VERSION})
Documentation=https://opentelemetry.io/docs/collector/
# network.target, not network-online.target: the exporter queues and retries, so
# the agent needn't wait (wait-online can stall boot for minutes on some hosts).
After=network.target $wants
Wants=$wants

[Service]
User=$AGENT_USER
Group=$AGENT_USER
SupplementaryGroups=$groups
EnvironmentFile=$ETC/agent.env
ExecStartPre=$PREFIX/bin/otelcol-contrib validate --config=$ETC/config.yaml
ExecStart=$PREFIX/bin/otelcol-contrib --config=$ETC/config.yaml
Restart=on-failure
RestartSec=10
StateDirectory=sentinel-agent
StateDirectoryMode=0750
CapabilityBoundingSet=
AmbientCapabilities=
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
LockPersonality=yes
SystemCallArchitectures=native
MemoryMax=400M
CPUQuota=50%

[Install]
WantedBy=multi-user.target
EOF
  # Make sure the config is valid with the real binary before switching over.
  # shellcheck disable=SC1091
  # From /: the Collector stats its working directory at start-up, and the
  # installer's (e.g. /root) is not readable by the agent user.
  ( cd / && set -a && . "$ETC/agent.env" && set +a
    runuser -u "$AGENT_USER" -- "$PREFIX/bin/otelcol-contrib" validate --config="$ETC/config.yaml" ) \
    || die "the Collector rejected the config; the previous agent (if any) keeps running"
  systemctl daemon-reload
  if [[ "$CONTAINERS" == 1 ]]; then
    systemctl enable sentinel-agent-socket-proxy.service >/dev/null 2>&1
    systemctl restart sentinel-agent-socket-proxy.service
  fi
  systemctl enable sentinel-agent.service >/dev/null 2>&1
  systemctl restart sentinel-agent.service
}

# ---------------------------------------------------------------- container mode
start_container() {
  local engine
  if command -v podman >/dev/null; then engine=podman
  elif command -v docker >/dev/null; then engine=docker
  else die "--mode container needs Podman or Docker"; fi
  state_set engine "$engine"

  log "Building the agent image with $engine (pinned base images)"
  curl -fsS --retry 5 --proto '=https,http' --max-time 30 -o "$WORK/Containerfile" "$ENDPOINT/agent/Containerfile" \
    || die "cannot download the agent Containerfile"
  # Remember which base images this installer pulls, so uninstall removes only those.
  local ref pulled
  pulled="$(sed -n 's/^pulled_images=//p' "$STATE")"
  for ref in $(awk '/^FROM /{print $2}' "$WORK/Containerfile") $([[ "$CONTAINERS" == 1 ]] && echo "$PROXY_IMAGE"); do
    if ! "$engine" image inspect "$ref" >/dev/null 2>&1 && [[ " $pulled " != *" $ref "* ]]; then
      pulled="${pulled:+$pulled }$ref"
    fi
  done
  state_set pulled_images "$pulled"
  # Podman keeps every build step as an image unless told not to (uninstall
  # must leave nothing behind).
  local build_flags=()
  [[ "$engine" == podman ]] && build_flags=(--layers=false)
  "$engine" build -q "${build_flags[@]}" -t "$AGENT_IMAGE" -f "$WORK/Containerfile" "$WORK" >/dev/null

  # In the container, uid 10001 must be able to read the (secret-free) config.
  chmod 0644 "$ETC/config.yaml"
  local journal_gid net=()
  journal_gid="$(getent group systemd-journal | cut -d: -f3 || true)"
  [[ -n "$NETWORK" ]] && net=(--network "$NETWORK")
  "$engine" rm -f sentinel-agent sentinel-agent-proxy >/dev/null 2>&1 || true

  local proxy_dir=/var/lib/sentinel-agent-proxy
  if [[ "$CONTAINERS" == 1 ]]; then
    # The socket's owner (root for rootful engines) with every capability dropped:
    # owner access to the engine socket and nothing else. Group 10001 = the agent.
    local owner_uid
    owner_uid="$(stat -c %u "$ENGINE_SOCKET")"
    # A host directory shared by the two containers: owned by the proxy, readable
    # by the agent's group only. A stale socket from an earlier run is removed.
    install -d -o "$owner_uid" -g 10001 -m 0750 "$proxy_dir"
    rm -f "$proxy_dir/engine.sock"
    "$engine" run -d --name sentinel-agent-proxy --restart=always \
      --user "$owner_uid:10001" --cap-drop=all --security-opt no-new-privileges \
      --security-opt label=disable --read-only --network none --memory 64m \
      -e SENTINEL_ENGINE_SOCKET=/engine.sock -e SENTINEL_PROXY_SOCKET=$PROXY_SOCKET \
      -v "$ENGINE_SOCKET:/engine.sock" -v "$proxy_dir:/run/sentinel-agent-proxy" \
      -v "$ETC/socket-proxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro" \
      "$PROXY_IMAGE" haproxy -db -f /usr/local/etc/haproxy/haproxy.cfg >/dev/null
  fi

  local extra=()
  [[ -n "$journal_gid" ]] && extra+=(--group-add "$journal_gid")
  [[ "$CONTAINERS" == 1 ]] && extra+=(-v "$proxy_dir:/run/sentinel-agent-proxy")
  "$engine" volume create sentinel-agent-data >/dev/null 2>&1 || true
  # uid/gid 10001: the gid must match the proxy socket's group (set above).
  "$engine" run -d --name sentinel-agent --restart=always --uts=host "${net[@]}" \
    --user 10001:10001 --cap-drop=all --security-opt no-new-privileges --security-opt label=disable \
    --read-only --memory 400m --cpus 0.5 \
    --env-file "$ETC/agent.env" \
    -v /:/hostfs:ro,rslave \
    -v "$ETC/config.yaml:/etc/sentinel-agent/config.yaml:ro" \
    -v sentinel-agent-data:/var/lib/sentinel-agent \
    "${extra[@]}" "$AGENT_IMAGE" >/dev/null
  # Restart the containers after a reboot.
  if [[ "$engine" == podman ]]; then
    systemctl enable podman-restart.service >/dev/null 2>&1 || true
  fi
}

if [[ "$MODE" == host ]]; then start_host; else start_container; fi

# ---------------------------------------------------------------- wait + verify
log "Waiting for the agent to start"
ok=0
for _ in $(seq 1 30); do
  if [[ "$MODE" == host ]]; then
    systemctl is-active --quiet sentinel-agent.service && curl -fs -o /dev/null "$SELF_METRICS" 2>/dev/null && { ok=1; break; }
  else
    # Running, and still the same run 10 s later (not a crash loop).
    engine="$(sed -n 's/^engine=//p' "$STATE")"
    first="$("$engine" inspect -f '{{.State.Running}} {{.RestartCount}} {{.State.StartedAt}}' sentinel-agent 2>/dev/null || true)"
    if [[ "$first" == true* ]]; then
      sleep 10
      [[ "$("$engine" inspect -f '{{.State.Running}} {{.RestartCount}} {{.State.StartedAt}}' sentinel-agent 2>/dev/null || true)" == "$first" ]] \
        && { ok=1; break; }
    fi
  fi
  sleep 1
done
if [[ "$ok" != 1 ]]; then
  if [[ "$MODE" == host ]]; then
    journalctl -u sentinel-agent.service -n 30 --no-pager >&2 || true
  else
    "$(sed -n 's/^engine=//p' "$STATE")" logs --tail 30 sentinel-agent >&2 || true
  fi
  die "the agent did not start"
fi
# Let the journald receiver reach the end of the journal before the marker.
sleep 5

NONCE="$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
logger -t sentinel-agent -p user.notice "sentinel-verify-$NONCE integration=$INTEGRATION"
state_set verify_nonce "$NONCE"

cat <<EOF

SENTINEL Agent ${OTELCOL_VERSION} is running ($MODE mode).
  integration   $INTEGRATION
  config hash   $CONFIG_HASH
  containers    $([[ "$CONTAINERS" == 1 ]] && echo "on, via GET-only proxy ($ENGINE_SOCKET)" || echo off)
  verify marker sentinel-verify-$NONCE (written to the journal just now)

SENTINEL shows the integration as healthy once metrics and logs arrive (about a minute).
Status:     $([[ "$MODE" == host ]] && echo "systemctl status sentinel-agent" || echo "$(sed -n 's/^engine=//p' "$STATE") logs sentinel-agent")
Uninstall:  sudo bash sentinel-agent-uninstall.sh   (from $ENDPOINT/agent/uninstall.sh)
EOF
