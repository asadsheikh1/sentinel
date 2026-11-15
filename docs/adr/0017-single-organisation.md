# ADR-0017: Single organisation per deployment

- **Status:** Proposed (pending plan approval)
- **Date:** 2026-11-14

## Context
The UX mentions creating an account. True multi-tenant SaaS needs tenant isolation in every store: Loki tenants, Prometheus (no native tenancy, so Mimir), and Postgres row-level security.

## Decision
Each SENTINEL deployment serves one organisation. Users are invited by an admin and public sign-up is off. All data carries `sentinel.integration.id`, which keeps a future tenancy migration possible.

## Consequences
Large scope and cost avoided. Not a SaaS product in v1.

## Alternatives considered
Multi-tenant from day one (high complexity, little portfolio benefit at this stage).
