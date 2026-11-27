# Telemetry stores (Step 5)

SENTINEL keeps raw telemetry in four stores. From Step 6, everything reaches them through the ingestion gateway. Here you can talk to them directly.

| Store | Holds | Local URL | Retention | Memory limit |
|---|---|---|---|---|
| Prometheus 3.16 | Metrics (OTLP receiver) | http://127.0.0.1:9090 | 15 days / 4 GB | 512 MiB |
| Loki 3.7 | Logs (OTLP endpoint) | http://127.0.0.1:3100 | 7 days | 512 MiB |
| Jaeger 2.23 | Traces (OTLP, Badger) | UI and API http://127.0.0.1:16686, OTLP :4317 / :4318 | 2 days | 768 MiB |
| Alertmanager 0.35 | Alerts (from Prometheus rules, Step 10) | http://127.0.0.1:9093 | 5 days | 128 MiB |

All ports are bound to `127.0.0.1`, so nothing is reachable from your network. Images are pinned by tag and digest in `deploy/compose/compose.observability.yaml`.

```bash
make obs-up       # start
make obs-ps       # status
make obs-stats    # memory use
make test-obs     # automated: push OTLP to each store and read it back
make test-rules   # promtool unit tests for the RED recording rules
make obs-down     # stop (data kept)
```

## Try it with curl

OTLP accepts JSON as well as protobuf, so plain curl works.

```bash
NOW=$(python3 -c 'import time; print(time.time_ns())')
TID=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
SID=$(python3 -c 'import secrets; print(secrets.token_hex(8))')
RES='{"attributes":[{"key":"service.name","value":{"stringValue":"curl-demo"}}]}'

# 1. A metric → Prometheus
curl -X POST http://127.0.0.1:9090/api/v1/otlp/v1/metrics -H 'Content-Type: application/json' \
  -d "{\"resourceMetrics\":[{\"resource\":$RES,\"scopeMetrics\":[{\"metrics\":[{\"name\":\"demo.temperature\",\"unit\":\"Cel\",\"gauge\":{\"dataPoints\":[{\"timeUnixNano\":\"$NOW\",\"asDouble\":21.5}]}}]}]}]}"

# 2. A log line → Loki
curl -X POST http://127.0.0.1:3100/otlp/v1/logs -H 'Content-Type: application/json' \
  -d "{\"resourceLogs\":[{\"resource\":$RES,\"scopeLogs\":[{\"logRecords\":[{\"timeUnixNano\":\"$NOW\",\"severityText\":\"ERROR\",\"body\":{\"stringValue\":\"hello from curl\"}}]}]}]}"

# 3. A span → Jaeger
curl -X POST http://127.0.0.1:4318/v1/traces -H 'Content-Type: application/json' \
  -d "{\"resourceSpans\":[{\"resource\":$RES,\"scopeSpans\":[{\"spans\":[{\"traceId\":\"$TID\",\"spanId\":\"$SID\",\"name\":\"curl-span\",\"kind\":2,\"startTimeUnixNano\":\"$NOW\",\"endTimeUnixNano\":\"$((NOW+5000000))\"}]}]}]}"
```

Read them back after a few seconds:

```bash
curl -s 'http://127.0.0.1:9090/api/v1/query?query=demo_temperature_celsius'
curl -s -G http://127.0.0.1:3100/loki/api/v1/query_range --data-urlencode 'query={service_name="curl-demo"}'
curl -s http://127.0.0.1:16686/api/v3/traces/$TID
```

Metric names follow the pinned translation (`UnderscoreEscapingWithSuffixes`): `demo.temperature` with unit `Cel` becomes `demo_temperature_celsius`. `service.name` and `service.namespace` become the `job` label. Selected resource attributes (`service_name`, `host_name`, `sentinel_integration_id`, …) are also copied onto every series as labels.

You can also browse: Prometheus at http://127.0.0.1:9090 and the Jaeger UI at http://127.0.0.1:16686.

## RED recording rules

`deploy/prometheus/rules/red.yml` precomputes per-service request rate, error rate, error ratio and p95 latency from OpenTelemetry's `http.server.request.duration` histogram. Detection rules built on them arrive in Step 10.
