# ADR-0002: Local inference via Ollama behind an internal LLM gateway

- **Status:** Proposed (pending plan approval)
- **Date:** 2026-11-14

## Context
Paid AI APIs are prohibited. Inference must run on an M4 (Metal) locally and on CPU in AWS.

## Decision
Use Ollama (MIT) as the runtime: native on macOS, container or native on EC2. All calls go through `sentinel.llm.gateway` using JSON-schema structured outputs. Model names are configuration.

## Consequences
The runtime can be swapped (llama.cpp server, vLLM later) without touching agent code. Models are compared through the evaluation harness. Containers on a Mac (inside the podman machine VM) cannot use Metal like native Ollama, so Ollama runs on the host.

## Alternatives considered
llama.cpp server directly (more manual model management); vLLM (needs a GPU); MLX-LM (Mac-only, so no AWS parity).
