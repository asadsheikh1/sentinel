# LLM gateway

Every model call in SENTINEL goes through `sentinel.llm` ([ADR-0002](../adr/0002-local-inference-ollama.md), [AI design §4.1](../2-architecture/3-ai-agents.md)). It talks to a local Ollama. There are no paid APIs.

## Running Ollama

```bash
ollama serve                      # native on the Mac, for Metal; listens on 127.0.0.1:11434
ollama pull qwen3:14b             # the default model (decision D2, see the benchmark)
ollama pull nomic-embed-text:v1.5 # embeddings
make test-ollama                  # the gateway against the real runtime (uses qwen3:4b)
make bench-models                 # the benchmark: docs/benchmarks/models-m4.md
```

From a container (the worker, from Step 17), Ollama on the Mac is `http://host.containers.internal:11434`.

## Using it

```python
from sentinel.llm import LLMGateway, DbCallSink, ollama_client

gateway = LLMGateway(ollama_client(settings), settings, DbCallSink(session_factory))
result = await gateway.generate_structured(
    "rca", RcaResult, messages, prompt_version="rca-v1", investigation_id=investigation.id
)
vectors = await gateway.embed(["search_document: ..."])
```

- **Structured output.** The Pydantic model's JSON schema is sent as Ollama's `format`, so the model can only produce JSON of that shape. The reply is then validated with Pydantic. If it doesn't validate, the gateway asks again: it sends back the reply and the validation errors, at most `SENTINEL_LLM_SCHEMA_RETRIES` (2) more times. After that it raises `LLMSchemaError`.
- **Runtime failures.** `LLMUnavailableError` covers a connection refused, a 5xx, or an error inside the stream. `LLMTimeoutError` covers no token within the stall timeout, or no full reply within the total timeout. Both are retried `SENTINEL_LLM_TRANSPORT_RETRIES` (1) times. `LLMHTTPError` (for example an unknown model) isn't retried. `LLMEmptyReplyError` means the model only thought and never answered.
- **Per task.** One model serves every task by default, so 24 GB never holds two. `SENTINEL_LLM_TASK_MODELS` overrides the model per task. Thinking is on for `rca`, `code_investigation` and `patch`, and off for everything else. `SENTINEL_LLM_TASK_THINKING` overrides it. A caller can also pass `model=` and `think=`.
- **Fixed sampling.** Every call uses `temperature` 0.1, a fixed `seed` (42) and `num_ctx` 16384. `num_predict` (8192, thinking included) caps runaway output.

## What is logged

| Where | What |
|---|---|
| `llm_calls` | One row per attempt, containing:<br>• task, model, prompt version, and the prompt's SHA-256 (not the prompt);<br>• thinking on or off, and `num_ctx`;<br>• input and output tokens, latency, time to first token, model load time;<br>• success, and the error kind and message.<br>The attempts of one `generate_structured` call share a `request_id`. |
| `llm_call_payloads` | The messages, the reply and the model's thinking. Only stored when `SENTINEL_LLM_STORE_PAYLOADS=true`. Deleted after `SENTINEL_LLM_PAYLOAD_RETENTION_DAYS` (30) by an hourly job. The read-only database role can't read it. |
| OpenTelemetry spans | `chat <model>` / `embeddings <model>`, with the GenAI attributes (`gen_ai.request.model`, `gen_ai.usage.*_tokens`, …) and `sentinel.llm.*` (task, attempt, thinking, TTFT). No prompt or reply text. |

A failure to write a log row is logged as a warning, and the call itself still succeeds.

## Tests

- **`FakeLLM({"rca": [reply, ...]})`** feeds scripted replies through the real gateway, so validation, retries and logging behave as in production.
  - A reply can be a model, a dict, raw text (to test a schema retry), an `LLMError` to raise, or a function of the request.
  - `.backend.requests` holds what was sent, and `.sink.calls` what was logged.
  - Embeddings are deterministic unit vectors.
- **`ReplayLLM(cassette)`** replays replies recorded from a real model. They're keyed by the exact request (model, messages, schema, options, thinking), so a changed prompt is a `ReplayMissError`, not a silent mismatch.
  - To record a cassette, wrap a real client in `ReplayBackend(path, mode="record", inner=ollama_client(settings))`.

## Settings

| Variable | Default | |
|---|---|---|
| `SENTINEL_OLLAMA_URL` | `http://127.0.0.1:11434` | |
| `SENTINEL_LLM_MODEL` | `qwen3:14b` | decision D2 |
| `SENTINEL_LLM_EMBED_MODEL` | `nomic-embed-text:v1.5` | 768 dimensions |
| `SENTINEL_LLM_TASK_MODELS`, `SENTINEL_LLM_TASK_THINKING` | `{}` | JSON |
| `SENTINEL_LLM_NUM_CTX` | 16384 | |
| `SENTINEL_LLM_MAX_OUTPUT_TOKENS` | 8192 | thinking included |
| `SENTINEL_LLM_TEMPERATURE`, `SENTINEL_LLM_SEED` | 0.1, 42 | |
| `SENTINEL_LLM_KEEP_ALIVE` | `30m` | how long Ollama keeps the model loaded |
| `SENTINEL_LLM_CONNECT_TIMEOUT_S`, `SENTINEL_LLM_STALL_TIMEOUT_S`, `SENTINEL_LLM_TIMEOUT_S` | 5, 180, 900 | the stall timeout covers loading the model |
| `SENTINEL_LLM_TRANSPORT_RETRIES`, `SENTINEL_LLM_SCHEMA_RETRIES` | 1, 2 | |
| `SENTINEL_LLM_STORE_PAYLOADS`, `SENTINEL_LLM_PAYLOAD_RETENTION_DAYS` | false, 30 | |
