# Reading telemetry safely (Step 7)

SENTINEL reads the stores only through three adapters in `backend/src/sentinel/telemetry/`. Later steps (evidence collection, investigation) use them too. Nothing else talks to Prometheus, Loki or Jaeger.

| Adapter | Reads | What callers can ask for |
|---|---|---|
| `PrometheusClient` | Metrics | A fixed **template** (`request_rate`, `error_rate`, `error_ratio`, `latency_p95`, `latency_p99`, `series`) over a selector and time window |
| `LokiClient` | Logs | Lines for a selector, with an optional text filter and minimum severity |
| `JaegerClient` | Traces | One trace by ID, or recent traces for a service (optionally errors only) |

## Safety rules

- **No raw queries.** Callers pass a `Selector` (service, namespace, environment, host, integration) and a `TimeRange`. Values may contain only identifier characters, and text filters are escaped. A value like `orders"} or vector(1)` is rejected before any request is made.
- **Caps on every read:**
  - 10 s timeout and 5 MB per response;
  - 50 series of 600 points;
  - 500 log lines of 2,000 characters;
  - 20 traces of 500 spans.

  Results say `truncated` when a cap was hit.
- **Everything returned is cleaned.** ANSI codes and control characters are stripped, and secrets are redacted, even if they slipped past the gateway. Trace IDs, span IDs, UUIDs and commit SHAs are kept. Each result reports what was redacted.

## Debug CLI

With the stack running (`make stack-up`):

```bash
cd backend
uv run sentinel telemetry metrics request_rate --namespace aviro --minutes 15
uv run sentinel telemetry metrics error_ratio --service orders
uv run sentinel telemetry logs --service orders --errors --limit 10
uv run sentinel telemetry logs --service orders --contains "dependency call failed"
uv run sentinel telemetry traces --service orders --errors --limit 5
uv run sentinel telemetry trace <trace_id>
```

Add `--json` for raw output. Add `--record file.json` to save the responses, or `--replay file.json` to answer from a saved file with no network.

## Redaction engine

`sentinel.security.redaction` covers:
- **Known formats:** private keys, AWS, GitHub, Slack, Stripe, Google and SENTINEL keys, and JWTs.
- **Auth headers.**
- **Credentials in URLs.**
- **`key=value` secrets:** `password`, `token`, `api_key` and similar.
- **Emails, and card numbers** (Luhn-checked).
- **High-entropy strings:** a fallback for unknown token formats.

The corpus in `backend/tests/fixtures/redaction/corpus.yaml` must be 100% redacted, and its benign lines must come through unchanged.
