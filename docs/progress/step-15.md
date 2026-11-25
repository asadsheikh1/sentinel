# Step 15 report: LLM gateway, call logging and the model benchmark

**Status:** ✅ built and tested; the benchmark has been measured. **D2 needs your decision** (see below).
**PR:** see the branch `step-15-llm-gateway`.
**Date:** 2026-11-25

## What was built

| Area | Files |
|---|---|
| Ollama client | `backend/src/sentinel/llm/ollama.py`:<br>• `/api/chat` is streamed, to measure time to the first token and to the first answer token. `/api/embed` returns embeddings. `/api/ps`, `/api/show` and unload are for inspection.<br>• Timeouts: connect, stall (the gap between streamed lines, which also covers loading a model) and total.<br>• A connection failure, a 5xx or a timeout is retried with backoff; a 4xx isn't. |
| Gateway | `llm/gateway.py`: `generate_structured(task, schema, messages, …)`.<br>• The Pydantic schema (with `$ref`s inlined) is sent as Ollama's `format`.<br>• Sampling: temperature 0.1, a fixed seed, `num_ctx` 16384, `keep_alive`, and an output cap.<br>• Thinking is on for `rca`, `code_investigation` and `patch` and off otherwise. Each can be overridden per task or per call. The model can be overridden per task.<br>• A reply that fails validation is sent back with the errors, at most 2 more times.<br>• Also `embed()`. |
| Logging | `llm/recorder.py`:<br>• one `llm_calls` row per attempt (tokens, latency, TTFT, load time, success, error kind);<br>• `llm_call_payloads` only with `SENTINEL_LLM_STORE_PAYLOADS=true`, pruned after 30 days by an hourly job;<br>• OpenTelemetry spans with the GenAI attributes and no prompt text.<br>A failure to log never breaks a call. |
| Fakes | `llm/fakes.py`: `FakeLLM` (scripted replies through the real gateway) and `ReplayLLM` / `ReplayBackend` (record from a real model, then replay; keyed by the exact request) |
| Database | Migration 0009: `llm_calls` and `llm_call_payloads`. The read-only role can't read payloads |
| Benchmark | `scripts/bench_models.py`, `make bench-models`; the report is [docs/benchmarks/models-m4.md](../benchmarks/models-m4.md) with its raw JSON |
| Other | `make test-ollama`; the `ollama` pytest marker, excluded by default and in CI; `sentinel.llm` sits between `events` and `telemetry` in the import layers, and the prober may not import it; settings in `.env.example`; [LLM gateway guide](../guides/llm-gateway.md) |

## Acceptance criteria (roadmap)

| Criterion | Result |
|---|---|
| A measured benchmark report | ✅ [models-m4.md](../benchmarks/models-m4.md). Qwen3 4B, 8B and 14B, plus nomic-embed-text v1.5, on this M4 with the SENTINEL stack running.<br>• Measured: tokens/s, time to first token, prefill, schema conformance, memory and swap.<br>• 14B and 8B were run again with the Podman VM stopped, to separate "slow" from "short of memory" |
| A default model recommendation for approval (D2) | ✅ **Recommended: Qwen3 8B.** Your approval is needed; until then the code default is unchanged (`qwen3:14b`) |
| Testing: mock-server unit tests | ✅ 43 tests in `tests/unit/test_llm_unit.py` (an `httpx` mock Ollama server, the gateway, the fakes) |
| Testing: a `requires_ollama` integration test, skipped in CI | ✅ `tests/integration/test_ollama_live.py`, 4 tests. Locally, against the real Ollama with `qwen3:4b`: 4 passed (structured RCA with thinking off and on, an unknown model gives a clear 404, embeddings rank a paraphrase above an unrelated text) |

## D2: what the benchmark found (details in the report)

- **Structured output is reliable on every model.** All 39 RCA runs gave valid JSON on the first try, with only real evidence IDs cited. Thinking works together with the JSON schema.
- **With the SENTINEL stack running, Qwen3 14B doesn't fit.**
  - macOS swap grew by 5.7 GB during its run.
  - It generated at about 5 tok/s.
  - One RCA call took 2–3.5 minutes, and it took 78 s just to read a 6k-token digest.
- **With the Podman VM stopped, 14B reaches 10.6 tok/s.** That's its ceiling on this machine.
- **Qwen3 8B: 18.7 tok/s with or without the stack**, swap barely moves, and one RCA call with thinking takes about 70 s.
- **On three smoke-test incidents, all three models gave the same answers.** That isn't a quality evaluation; Step 26 does that.
- **Qwen3 4B thinks for about 7,000 tokens** (4 minutes) when thinking is on.

**Recommendation: Qwen3 8B as the default for every task, with thinking on for RCA. nomic-embed-text v1.5 for embeddings.**
- 14B stays one setting away.
- D2 can be revisited when Step 26 compares quality.

If you approve, I'll change the default and update D2 in `docs/README.md` and AI design §5, at the start of Step 16 or as a small follow-up commit, whichever you prefer.

## Test results

| Suite | Result |
|---|---|
| `make lint` (ruff, format, mypy strict, including `scripts/`, import contracts) | Clean |
| `make test-all` (unit + db + integration) | 755 passed, coverage 94.0%. 5 new db tests: attempt logging, failures, payloads and retention, logging never breaking a call, read-only privileges. The migration cycle now includes the new tables. |
| `make test-ollama` (real Ollama, `qwen3:4b`) | 4 passed |
| End to end against the dev database | A real `qwen3:4b` call through the gateway with the database sink: the `llm_calls` row (tokens 35/28, latency 2.2 s, TTFT 1.3 s, load 1.0 s) and its payload row; an embedding row |

## Bugs found and fixed during this step

1. **One gateway instance kept the current attempt's record on itself.** Two calls running at the same time could log each other's results. Each attempt now returns its own record, and a test runs 5 calls concurrently.
2. **The benchmark's "6k-token" digest was really 12.4k tokens.** Log-like text tokenises at about 2 characters per token, not 4. The generator is now calibrated (measured: 6,150–6,290 tokens).
3. **The live tests left HTTP connections open**, which pytest reports as an error. The fixture now closes its client.

## Decisions worth reviewing

1. **One row per attempt.** `llm_calls` has a few columns beyond the design's list:
   - `request_id` and `attempt` show retries;
   - `operation` is chat or embed;
   - `thinking`, `num_ctx` and `load_ms` explain latency;
   - `error_kind` makes failures countable.
2. **`investigation_id` has no foreign key yet.** The `investigations` table arrives in Step 17, and the key with it.
3. **Requests are streamed**, so time to first token is measured on every call, not only in the benchmark.
4. **`$ref`s are inlined in the schema sent to Ollama**, and recursive schemas are refused. Pydantic still validates everything the grammar can't express.
5. **`LLMEmptyReplyError`** is raised when a model thinks but never answers (for example, it hits the output cap).
6. **The OpenTelemetry API is now a direct dependency** (it was already installed through FastAPI). There's no exporter yet, so spans go nowhere until SENTINEL's own tracing is wired. The tests use the SDK's in-memory exporter.
7. **CI now lints `scripts/`**, and `ci-backend` runs when anything in `scripts/` changes.

## Known limitations

- **Nothing calls the gateway in production yet.** The investigator (Step 17) is the first caller. `/readyz` doesn't check Ollama: CI has none, and design says a missing Ollama degrades investigations, not the API.
- **The benchmark's quality check is three cases.** Model quality is decided by the evaluation suite (Step 26).
- **Only Qwen3 4B, 8B and 14B were benchmarked.** Qwen2.5-Coder (optional, for patches) and bge-m3 (an embedding fallback) weren't; the design only needs them if the evaluation shows a gain.

## How you verify

```bash
cd "/Users/asadsheikh/Coding/SENTINEL Autonomous AI Site Reliability Engineer"
git checkout step-15-llm-gateway
ollama serve                                   # in another terminal
make test-ollama                               # about 2.5 minutes with qwen3:4b
make bench-models ARGS="--models qwen3:8b --runs 1 --no-think --out /tmp/bench.json"   # about 3 minutes
```

The full benchmark is `make bench-models` (about 1.5 hours with the stack up).
