# Model benchmark on the M4 (Step 15, decision D2)

**Measured:** 2026-11-25, with `make bench-models`. The raw results are in [models-m4.json](models-m4.json) (the SENTINEL stack running) and [models-m4-without-podman.json](models-m4-without-podman.json) (the Podman VM stopped).

**Machine:**
- Apple M4, 24 GB, macOS 26.6.2.
- Ollama 0.40.16, native (Metal).
- Podman VM: 8 GB, 4 CPUs, with the full SENTINEL stack running (9 containers) unless stated otherwise.
- The IDE and a browser were open, as in normal use.

**Settings:** SENTINEL's gateway defaults:
- `num_ctx` 16384, temperature 0.1, schema retries 2;
- the output cap is 8192 tokens, thinking included;
- every model is Q4_K_M from the Ollama library, Apache-2.0.

## What was measured

Everything was measured through SENTINEL's own gateway (`sentinel.llm`), so these are the numbers SENTINEL itself will see.

1. **Cold load:** all models are unloaded, then one small request is sent.
2. **Memory:**
   - Ollama's own size of the loaded model at a 16k context (`/api/ps`; all of it in GPU memory for every model);
   - macOS free memory and swap, before and after each model.
3. **Prefill:** one prompt of about 6,200 tokens that the model has never seen, so there's no prompt cache. That's the size of the evidence digest the RCA analyst will get.
4. **Structured RCA:** three synthetic incidents, each with one known cause and some noise. The schema is shaped like `RcaResult` (ranked hypotheses, a category enum, evidence IDs, confidence).
   - The incidents: a bad deploy (`code_change`), a full database disk (`resource_exhaustion`), and a config change (`config_change`).
   - Thinking off: 2 seeds per incident. Thinking on: 1 seed per incident.
   - Recorded for each run:
     - whether the JSON was valid on the first try, and after retries;
     - whether every cited evidence ID exists;
     - whether the top hypothesis has the right category;
     - generation speed, time to the first answer token, latency, and output tokens.

The category check is **a smoke test of three cases, not a quality evaluation**. Quality is measured on the scenario suite (Step 26).

## Results with the SENTINEL stack running (the normal setup)

| Model | Size in memory (16k ctx) | Cold load | Prefill | First token of a 6.2k prompt | Swap before → after |
|---|---|---|---|---|---|
| Qwen3 4B | 4.7 GB | 0.8 s | 324 tok/s | 19 s | 2.9 → 3.6 GB |
| Qwen3 8B | 6.5 GB | 3.5 s | 185 tok/s | 33 s | 3.6 → 3.9 GB |
| **Qwen3 14B** | **10.9 GB** | 5.8 s | **80 tok/s** | **78 s** | **3.9 → 9.4 GB** |

| Model | Thinking | Valid JSON, first try | Citations valid | Right category | Generation | First answer token | Latency, median (max) | Output tokens, median |
|---|---|---|---|---|---|---|---|---|
| Qwen3 4B | off | 6/6 | 6/6 | 4/6 | 34.8 tok/s | 0.5 s | 20 s (22 s) | 678 |
| Qwen3 4B | on | 3/3 | 3/3 | 2/3 | 29.4 tok/s | 218 s | 236 s (249 s) | 6,917 |
| Qwen3 8B | off | 6/6 | 6/6 | 4/6 | 18.7 tok/s | 0.9 s | 45 s (49 s) | 818 |
| Qwen3 8B | on | 3/3 | 3/3 | 2/3 | 18.7 tok/s | 34 s | 71 s (73 s) | 1,322 |
| **Qwen3 14B** | off | 6/6 | 6/6 | 4/6 | **5.3 tok/s** | 1.8 s | **135 s (165 s)** | 683 |
| **Qwen3 14B** | on | 3/3 | 3/3 | 2/3 | **6.1 tok/s** | 95 s | **166 s (207 s)** | 882 |

## Qwen3 14B and 8B with the Podman VM stopped

This run checks whether 14B is slow on its own, or slow because the machine is out of memory. One seed per incident, the same prompts:

| Model | Generation | Prefill | Latency, thinking off | Latency, thinking on | Swap before → after |
|---|---|---|---|---|---|
| Qwen3 14B | 5.7–6.0 tok/s for the first two replies, then **10.6–10.7 tok/s** | 55 tok/s (first request, while paging back in) | 70–138 s | 79–146 s | 3.0 → 1.8 GB (paging back in) |
| Qwen3 8B | 18.5–18.7 tok/s | 177 tok/s | 42–46 s | 66–77 s | 1.8 → 1.8 GB |

## Embeddings: nomic-embed-text v1.5

| Dimensions | In memory | Cold load | Batch of 64 | Single text (median) | Similarity: paraphrase vs unrelated |
|---|---|---|---|---|---|
| 768 | 320 MB | 0.5 s | 95–124 texts/s | 9–13 ms | 0.60 vs 0.46 |

## What the numbers say

1. **Structured output works on every model.** Every reply was valid JSON against the schema on the first try (all 27 RCA runs with the stack up, and all 12 without). No retry was needed. Every cited evidence ID existed. Thinking and the JSON schema work together: the model thinks in a separate field, then writes JSON constrained to the schema.
2. **On this smoke test, 14B was no more accurate than 8B or 4B.** All three got the same 2 of 3 categories right. All three called the full disk `infrastructure` instead of `resource_exhaustion`; that's arguable, since the evidence also mentions failing WAL archiving. Three cases say nothing about quality in general: Step 26 measures that.
3. **14B doesn't fit next to the stack.**
   - With the Podman VM (8 GB) running, swap grew by 5.7 GB over the 14B run (10.9 GB loaded).
   - It generated at about 5 tok/s, half of what it does when it has the memory, and its speed varied from run to run (3.5–7.5 tok/s).
   - So one RCA call takes about 2–3.5 minutes, and an investigation (Step 17) makes several calls.
4. **Even with all the memory, 14B gives about 10.6 tok/s.** That's what the M4's memory bandwidth allows for a 9 GB model. Reading a 6k-token digest takes over a minute before the first token.
5. **8B is steady:**
   - 18.7 tok/s with or without the stack, and swap barely moves;
   - with thinking on, it thinks briefly (about 500 tokens) and answers in about 70 s.
6. **4B thinks far too long.** With thinking on, it produced about 7,000 tokens per answer: about 4 minutes, close to the 8,192-token cap. With thinking off it's the fastest (20 s), but it's the weakest model on paper.

## Recommendation for D2 (needs your approval)

**Default model: Qwen3 8B, with thinking on for RCA.** The design said "Qwen3 14B, to be confirmed after a speed test in step 15". The test doesn't confirm it on this machine:

- With SENTINEL running, 14B swaps and runs at about 5 tok/s.
- 8B is about 3.5 times faster with the stack up, uses 4.4 GB less memory, and is steady.
- It was just as reliable here: all JSON valid on the first try, and the same results on the smoke test.

That's the fallback the design already names (AI design §5, risk R3).

The model stays a single setting (`SENTINEL_LLM_MODEL`). 14B remains available, for example:
- on a machine with more memory;
- with the Podman VM capped lower;
- for one task (`SENTINEL_LLM_TASK_MODELS`).

Step 26 (the evaluation suite) compares 8B and 14B on quality, which this benchmark doesn't measure. D2 can be revisited then.

**Embeddings: nomic-embed-text v1.5**, as designed (768-d, 320 MB, about 10 ms per text).

Until you decide, the code's default is unchanged (`qwen3:14b`, as in the approved design).

## Reproduce

```bash
ollama serve
ollama pull qwen3:14b && ollama pull qwen3:8b && ollama pull qwen3:4b && ollama pull nomic-embed-text:v1.5
make stack-up                                   # the normal setup
make bench-models                               # all candidates; about 1.5 hours
make bench-models ARGS="--models qwen3:8b --runs 1 --no-think"   # quick check
```
