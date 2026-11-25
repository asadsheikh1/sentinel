#!/usr/bin/env bash
# The AVIRO host for the evaluation suite: a disposable Ubuntu 24.04 machine on
# SENTINEL's ingest network, running AVIRO inside its own container engine.
#
#   evals/host/aviro-host.sh up TOKEN     # machine + disk + AVIRO (telemetry sent with TOKEN)
#   evals/host/aviro-host.sh down         # remove the machine and its disk
#   evals/host/aviro-host.sh status
#   evals/host/aviro-host.sh exec CMD...  # run a command on the machine
#
# AVIRO's disk (/var/lib/containers on the machine) is a 6 GiB ext4 filesystem of
# its own, made as a loop device in the Podman VM (rootless containers can't make
# loop devices). S13 fills it; SENTINEL's stores are on a different disk.
#
# Env: AVIRO_REPO (default ../aviro-shop next to this repo), AVIRO_REF (default main),
#      SENTINEL_AGENT_PACKAGE_DIR (optional: cached agent downloads, mounted at /pkg).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
MACHINE=aviro-host
IMAGE=localhost/sentinel-aviro-host:24.04
BASE=localhost/sentinel-test-ubuntu:24.04
AVIRO_REPO="${AVIRO_REPO:-$(cd "$ROOT/.." && pwd)/aviro-shop}"
# The working copy on the host (compose files, load generator). Deploys pick their own ref.
AVIRO_REF="${AVIRO_REF:-main}"
VM_DIR=/var/lib/sentinel-eval
VM_MNT=/var/mnt/aviro-host-disk
DISK_SIZE=6G
# Base images AVIRO's build and compose use: copied in, not downloaded again.
PRELOAD=(
  docker.io/library/python:3.12-slim-trixie
  ghcr.io/astral-sh/uv:0.13.8
  docker.io/library/postgres:17.12-alpine
  docker.io/valkey/valkey:8.1.11-alpine
  docker.io/locustio/locust:2.47.0
)

vm() { podman machine ssh "$*"; }
on() { podman exec "$MACHINE" "$@"; }
on_in() { podman exec -i "$MACHINE" "$@"; }  # feeds stdin
log() { printf '==> %s\n' "$*" >&2; }

disk_up() {
  vm "sudo mkdir -p $VM_DIR $VM_MNT && \
      { test -f $VM_DIR/disk.img || { sudo truncate -s $DISK_SIZE $VM_DIR/disk.img && sudo mkfs.ext4 -q -F -L aviro-data $VM_DIR/disk.img; }; } && \
      { mountpoint -q $VM_MNT || sudo mount -o loop $VM_DIR/disk.img $VM_MNT; } && \
      sudo chown \$(id -u):\$(id -g) $VM_MNT"
}

disk_down() {
  vm "if mountpoint -q $VM_MNT; then sudo umount $VM_MNT; fi; \
      for dev in \$(sudo losetup -j $VM_DIR/disk.img -O NAME --noheadings); do sudo losetup -d \$dev; done; \
      sudo rm -f $VM_DIR/disk.img"
}

wait_systemd() {
  for _ in $(seq 1 120); do
    # is-system-running exits non-zero when degraded (some kernel mounts can't exist here).
    if { on systemctl is-system-running 2>/dev/null || true; } | grep -qE 'running|degraded'; then return 0; fi
    sleep 1
  done
  echo "the machine did not boot" >&2; return 1
}

up() {
  local token="${1:?usage: aviro-host.sh up TOKEN}"
  podman image exists "$BASE" || podman build -q -t "$BASE" -f "$ROOT/backend/tests/agent/Containerfile.systemd" "$ROOT/backend/tests/agent" >/dev/null
  podman build -q -t "$IMAGE" -f "$HERE/Containerfile" "$HERE" >/dev/null
  log "AVIRO's disk ($DISK_SIZE ext4) in the Podman VM"
  disk_up
  log "starting the machine"
  podman rm -f "$MACHINE" >/dev/null 2>&1 || true
  local mounts=(-v "$VM_MNT:/var/lib/containers")
  if [[ -n "${SENTINEL_AGENT_PACKAGE_DIR:-}" ]]; then mounts+=(-v "$SENTINEL_AGENT_PACKAGE_DIR:/pkg:ro"); fi
  podman run -d --name "$MACHINE" --hostname "$MACHINE" --network sentinel-ingest \
    --privileged "${mounts[@]}" "$IMAGE" >/dev/null
  wait_systemd
  on findmnt -no FSTYPE /var/lib/containers | grep -qx ext4 || { echo "AVIRO's disk is not mounted" >&2; return 1; }

  log "copying AVIRO (all branches and scenario tags)"
  local bundle
  bundle="$(mktemp)"
  git -C "$AVIRO_REPO" bundle create "$bundle" --all 2>/dev/null
  on_in sh -c 'cat > /srv/aviro.bundle' < "$bundle"
  rm -f "$bundle"
  on env AVIRO_REF="$AVIRO_REF" sh -c 'rm -rf /srv/aviro && git clone -q /srv/aviro.bundle /srv/aviro && cd /srv/aviro && git checkout -q main && git checkout -q "$AVIRO_REF" && git fetch -q --tags /srv/aviro.bundle "+refs/heads/*:refs/remotes/bundle/*"'

  log "loading base images"
  for img in "${PRELOAD[@]}"; do
    if ! on podman image exists "$img"; then podman save "$img" | on_in podman load -q >/dev/null; fi
  done

  log "starting AVIRO"
  on podman network exists sentinel-ingest || on podman network create sentinel-ingest >/dev/null
  # deploy.sh reads .sentinel.env: telemetry goes to SENTINEL's ingest endpoint with
  # the app token. No webhook here: the harness posts the deployment events.
  on sh -c "umask 077; printf 'AVIRO_SENTINEL_TOKEN=%s\n' '$token' > /srv/aviro/.sentinel.env"
  on sh -c 'cd /srv/aviro && podman-compose -f compose.yaml -f compose.sentinel.yaml up -d postgres valkey >/dev/null 2>&1'
  on sh -c 'cd /srv/aviro && scripts/deploy.sh main'
  for _ in $(seq 1 90); do
    if on curl -fsS http://127.0.0.1:8081/api/health 2>/dev/null | grep -q '"ready":true'; then
      log "AVIRO is up on http://$MACHINE:8081"
      return 0
    fi
    sleep 2
  done
  echo "AVIRO did not become ready" >&2; return 1
}

down() {
  podman rm -f "$MACHINE" >/dev/null 2>&1 || true
  disk_down
  log "removed the machine and its disk"
}

status() {
  podman ps -a --filter "name=^${MACHINE}$" --format '{{.Names}} {{.Status}}'
  on sh -c 'df -h /var/lib/containers | tail -1; cd /srv/aviro && podman-compose ps 2>/dev/null | tail -n +1' || true
}

cmd="${1:-}"; shift || true
case "$cmd" in
  up) up "$@" ;;
  down) down ;;
  status) status ;;
  exec) on_in "$@" ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
