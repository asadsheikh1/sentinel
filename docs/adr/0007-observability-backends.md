# ADR-0007: Prometheus, Loki, and Jaeger behind an OTel Collector

- **Status:** Proposed (pending plan approval)
- **Date:** 2026-11-14

## Context
SENTINEL must query logs, metrics, and traces, but building telemetry storage is out of scope.

## Decision
Apps emit OTLP to an OTel Collector, which routes to Prometheus (metrics), Loki (logs, AGPL, unmodified), and Jaeger (traces). SENTINEL queries them through typed adapters.

## Consequences
Industry-standard and free. Loki's AGPL obligations apply only to modified, network-offered copies. Backends are swappable behind the adapters (for example Tempo or OpenSearch).

## Alternatives considered
OpenSearch for logs (heavy); SigNoz or ClickHouse-based stacks (heavier); logs in Postgres (poor query ergonomics).
