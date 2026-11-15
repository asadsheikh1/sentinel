# ADR-0003: PostgreSQL + pgvector as the single datastore

- **Status:** Proposed (pending plan approval)
- **Date:** 2026-11-14

## Context
We need relational data, a small job queue, workflow checkpoints, and vector similarity over a few thousand incidents at most.

## Decision
Use PostgreSQL 17 with pgvector (HNSW) for all of them. Raw telemetry stays in Loki, Prometheus, and Jaeger. Only redacted evidence snapshots are stored.

## Consequences
One backup target and transactional consistency between incidents and embeddings. A vector DB would be overkill at this scale.

## Alternatives considered
Qdrant, Chroma, or Weaviate (an extra service); Redis (licence and extra infrastructure).
