# ADR-0012: Server-enforced evidence citations for every hypothesis

- **Status:** Proposed (pending plan approval)
- **Date:** 2026-11-14

## Context
Evidence-based investigation is the core product principle. LLMs can fabricate references.

## Decision
Evidence is stored immutably with incident-scoped IDs. LLM outputs must cite IDs. A validator strips invalid IDs and drops uncited hypotheses. Raw citation validity is tracked as a model-quality metric.

## Consequences
Users can always click through to the data. Fabricated citations are impossible to display. Some investigations may end up 'degraded' with no hypotheses, which is better than an unsupported claim.

## Alternatives considered
Trusting LLM citations (rejected); retrieval-only summaries with no hypotheses (too weak).
