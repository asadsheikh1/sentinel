# ADR-0014: OTLP-first ingestion through one authenticated gateway

- **Status:** Proposed (pending plan approval)
- **Date:** 2026-11-14

## Context
There are many sources (agents, SDKs, probes, connectors). Separate pipelines per platform would multiply code and inconsistencies.

## Decision
All telemetry is normalised to OTLP with OpenTelemetry semantic conventions and enters through `ingest.<domain>`: Caddy `forward_auth` → sentinel-api (token → integration, rate limit, last-seen) → gateway Collector (identity overwrite, OTTL normalisation, redaction, cardinality guard) → Prometheus, Loki, and Jaeger via OTLP. Discrete events use the `SentinelEvent` schema in Postgres.

## Consequences
Detection and AI are source-agnostic. Last-seen tracking costs nothing extra. Per-request auth adds latency, which is acceptable at MVP scale; a custom Collector auth extension is the fallback.

## Alternatives considered
Collector `bearertokenauth` with a static token list (no per-integration identity or revocation without restart); separate ingestion services per source type.
