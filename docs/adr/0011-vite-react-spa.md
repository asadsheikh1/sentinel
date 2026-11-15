# ADR-0011: Vite + React SPA instead of Next.js

- **Status:** Proposed (pending plan approval)
- **Date:** 2026-11-14

## Context
The UI is an authenticated internal tool with no SEO or SSR needs.

## Decision
Build a Vite + React + TypeScript SPA served statically by Caddy, with an API client generated from OpenAPI.

## Consequences
No Node server in production, so less memory and attack surface. Simple deploys.

## Alternatives considered
Next.js (unneeded SSR); HTMX/Jinja (weaker for rich evidence views and diffs).
