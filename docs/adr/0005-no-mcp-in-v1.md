# ADR-0005: No MCP inside SENTINEL for v1

- **Status:** Proposed (pending plan approval)
- **Date:** 2026-11-14

## Context
MCP standardises tool exposure between LLM clients and servers. All of SENTINEL's tools are internal and known at build time.

## Decision
Do not use MCP internally. Tools are typed Python functions behind a small `Tool` interface. A read-only MCP server exposing incidents and evidence to external IDE agents is a Could Have (P14).

## Consequences
Less protocol overhead and attack surface. Small local models work better with constrained JSON schemas. MCP stays easy to add as an outward-facing adapter.

## Alternatives considered
MCP for all internal tools (no benefit, more processes).
