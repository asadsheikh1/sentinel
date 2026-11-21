#!/usr/bin/env bash
# SENTINEL Agent uninstaller. Removes everything agent/install.sh created:
# services, containers and images, files, the sentinel-agent user, and HAProxy if
# the installer added it. Telemetry already sent to SENTINEL is kept there; revoke
# the integration's token in SENTINEL to make sure the old token can't be reused.
#
#   sudo bash sentinel-agent-uninstall.sh

set -Euo pipefail

PREFIX=/opt/sentinel-agent
ETC=/etc/sentinel-agent
STATE="$PREFIX/state"
AGENT_USER=sentinel-agent

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
[[ $EUID -eq 0 ]] || { echo "error: run as root (sudo bash $0)" >&2; exit 1; }

get() { [[ -r "$STATE" ]] && sed -n "s/^$1=//p" "$STATE" | tail -1; return 0; }
mode="$(get mode)"
engine="$(get engine)"
installed_haproxy="$(get installed_haproxy)"
pulled_images="$(get pulled_images)"

if [[ -d /run/systemd/system ]]; then
  for unit in sentinel-agent.service sentinel-agent-socket-proxy.service; do
    if systemctl list-unit-files "$unit" >/dev/null 2>&1; then
      log "Stopping $unit"
      systemctl disable --now "$unit" >/dev/null 2>&1 || true
    fi
    rm -f "/etc/systemd/system/$unit"
  done
  systemctl daemon-reload
  systemctl reset-failed sentinel-agent.service sentinel-agent-socket-proxy.service >/dev/null 2>&1 || true
fi

for e in ${engine:-podman docker}; do
  command -v "$e" >/dev/null || continue
  if "$e" container exists sentinel-agent >/dev/null 2>&1 || "$e" inspect sentinel-agent >/dev/null 2>&1 \
     || "$e" inspect sentinel-agent-proxy >/dev/null 2>&1; then
    log "Removing the agent containers ($e)"
  fi
  "$e" rm -f sentinel-agent sentinel-agent-proxy >/dev/null 2>&1 || true
  "$e" volume rm -f sentinel-agent-data sentinel-agent-proxy >/dev/null 2>&1 || true
  "$e" image rm -f "$("$e" images -q localhost/sentinel-agent 2>/dev/null | head -1)" >/dev/null 2>&1 || true
  # Anything else built from agent/Containerfile (it carries this label).
  for id in $("$e" images -a -q --filter "label=org.opencontainers.image.title=SENTINEL Agent" 2>/dev/null | sort -u); do
    "$e" image rm -f "$id" >/dev/null 2>&1 || true
  done
  # Only base images the installer pulled itself; images that were already here stay.
  for ref in $pulled_images; do
    "$e" image rm "$ref" >/dev/null 2>&1 || true
  done
done

if [[ "$installed_haproxy" == 1 ]]; then
  log "Removing HAProxy (installed by the SENTINEL installer)"
  if command -v apt-get >/dev/null; then
    DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq haproxy >/dev/null 2>&1 || true
    DEBIAN_FRONTEND=noninteractive apt-get autoremove -y -qq >/dev/null 2>&1 || true
  elif command -v dnf >/dev/null; then
    dnf remove -y -q haproxy >/dev/null 2>&1 || true
  fi
fi

log "Removing files"
rm -rf "$ETC" "$PREFIX" /var/lib/sentinel-agent /var/lib/sentinel-agent-proxy /run/sentinel-agent-proxy

if id -u "$AGENT_USER" >/dev/null 2>&1; then
  log "Removing user $AGENT_USER"
  userdel "$AGENT_USER" >/dev/null 2>&1 || true
  getent group "$AGENT_USER" >/dev/null && groupdel "$AGENT_USER" >/dev/null 2>&1
fi

echo
echo "SENTINEL Agent removed${mode:+ ($mode mode)}. Revoke the integration's token in SENTINEL if it is no longer used."
