# ADR-0004: LangGraph only for investigation and repair workflows

- **Status:** Proposed (pending plan approval)
- **Date:** 2026-11-14

## Context
Some workflows are multi-step, conditional, looping, and must be resumable. Others are deterministic and security-critical.

## Decision
Use LangGraph's core StateGraph with the Postgres checkpointer for the RCA investigation (with the code subgraph) and the code repair loop only. Detection, evidence collection, approvals, and execution are plain Python.

## Consequences
Graph complexity is spent only where it pays off. Security-critical paths are never LLM-driven. Limited exposure to framework churn.

## Alternatives considered
LangGraph everywhere (overuse); a hand-rolled state machine for investigations (reinvents checkpointing and streaming).
