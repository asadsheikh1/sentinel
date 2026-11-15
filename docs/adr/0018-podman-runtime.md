# ADR-0018: Podman as the container runtime (no Docker)

- **Status:** Accepted (decided by the project owner, 2026-11-15)
- **Date:** 2026-11-15

## Context
SENTINEL, its telemetry stack, the AVIRO demo shop, and the test sandboxes all run in containers. The project owner does not want Docker Desktop installed because of its size, and prefers an open-source runtime.

## Decision
Use **Podman** (Apache-2.0) everywhere:
- macOS development uses `podman machine` (a small Linux VM, 4 CPU / 8 GB / 60 GB) with `podman compose` backed by **podman-compose**.
- CI uses Podman, which is preinstalled on GitHub's Ubuntu runners. Multi-arch images are built with `podman build --platform linux/amd64,linux/arm64 --manifest`.
- AWS EC2 runs **rootless Podman** under a dedicated service user, using the same Compose files. Quadlet (systemd) units are the fallback if podman-compose limitations appear.
- The runner talks to the rootless Podman API socket through `podman-py`.
- Testcontainers points `DOCKER_HOST` at the Podman socket with `TESTCONTAINERS_RYUK_DISABLED=true`.
- Images are defined in `Containerfile`s.
- **Monitored systems may still use Docker.** The agent supports both engines through their Docker-compatible API.

## Consequences
- **Security improves:** containers and the runner are rootless and daemonless, so a runner or sandbox escape does not give host root.
- No Docker Desktop licence questions.
- Some tools assume Docker. A compatibility spike in Step 1 checks Compose features, Testcontainers, and multi-arch builds. Compose files stay within the portable subset.
- Ollama still runs natively on the Mac, outside Podman, for Metal acceleration.

## Alternatives considered
Docker Desktop (large, commercial terms for organisations); Colima + Docker CLI (still the Docker toolchain); OrbStack (proprietary).
