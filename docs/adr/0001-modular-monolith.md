# ADR-0001: Modular monolith with a separate privileged runner

- **Status:** Proposed (pending plan approval)
- **Date:** 2026-11-14

## Context
SENTINEL is built by one developer, uses one database, and has no independent scaling needs. Microservices would add network failure modes and operational cost.

## Decision
Build one Python codebase deployed as `api`, `worker`, and `scheduler` processes sharing PostgreSQL. Container-engine (Podman) access goes in a separate minimal `runner` service, because it needs a different privilege level.

## Consequences
Simple deploys and transactional consistency. Module boundaries are enforced with import-linter instead of network boundaries. Processes can be split out later if needed.

## Alternatives considered
Microservices per capability; a single process including container-engine access (rejected for security).
