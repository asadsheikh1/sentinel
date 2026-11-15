# ADR-0008: Privileged runner broker for all container operations

- **Status:** Proposed (pending plan approval)
- **Date:** 2026-11-14

## Context
Sandbox test runs and restart or rollback actions need the container engine API socket. With rootful engines this is root-equivalent on the host. With **rootless Podman** it is equivalent to the runner's unprivileged user, which is less severe but still powerful.

## Decision
Only `sentinel-runner` mounts the socket. It exposes fixed, allowlisted operations over an internal HMAC-signed API, has no LLM dependency, and reads targets from its own allowlist file.

## Consequences
A compromised worker cannot run arbitrary containers or commands. The privileged code is small and auditable.

## Alternatives considered
Mounting the socket in the worker (rejected); a separate runner per concern (possible later split).
