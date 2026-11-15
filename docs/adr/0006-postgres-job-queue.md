# ADR-0006: Postgres-backed job queue instead of Celery/Redis

- **Status:** Proposed (pending plan approval)
- **Date:** 2026-11-14

## Context
SENTINEL runs tens to hundreds of jobs a day. Enqueueing must be atomic with domain writes (for example, incident created → investigation enqueued).

## Decision
Use a small in-house queue: `jobs` table, `SELECT … FOR UPDATE SKIP LOCKED`, heartbeat, retry with backoff, dead-letter, LISTEN/NOTIFY wake-up.

## Consequences
No broker to run. Transactional outbox semantics for free. The code is small and fully tested, including concurrency tests.

## Alternatives considered
Celery + Redis/Valkey (more infrastructure); Procrastinate (a reasonable MIT alternative, kept as a fallback); arq (needs Redis).
