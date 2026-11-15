# ADR-0015: In-house SSRF-guarded prober for external health checks

- **Status:** Proposed (pending plan approval)
- **Date:** 2026-11-14

## Context
Users configure probe targets. A naive prober running inside SENTINEL's network can be pointed at internal services or the cloud metadata endpoint.

## Decision
Implement a small Python prober in its own container on an egress-only network. It resolves the target, validates the IPs against blocked ranges, pins the connection to the vetted IP, and re-validates redirects. Results are emitted as OTLP metrics.

## Consequences
Full control over SSRF rules and testability. A little more code than configuring blackbox_exporter.

## Alternatives considered
Prometheus blackbox_exporter (cannot enforce resolve-validate-pin per target); a third-party uptime service (external dependency).
