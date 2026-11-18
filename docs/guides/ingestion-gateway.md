# Ingestion gateway (Step 6)

Every monitored system sends telemetry through one authenticated front door.

```
your app ──OTLP + token──▶ Caddy :4319 ──▶ gateway Collector ──▶ Prometheus / Loki / Jaeger
                             │
                             └─ asks sentinel-api: is this token valid?
```

| Stage | What happens |
|---|---|
| **Caddy** | Accepts only `POST /v1/metrics`, `/v1/logs` and `/v1/traces`. Removes client-sent identity headers. Removes the token before forwarding |
| **`/internal/ingest-auth`** | Checks the token, enforces its per-minute limit, and records when each signal was last seen |
| **Gateway Collector** | Overwrites identity from the token, normalises attributes and severities, redacts secrets, drops unbounded metric labels |

## Connect a system

```bash
make stack-up                                    # everything, ingest on 127.0.0.1:4319
make create-admin email=you@example.com name="You"
curl -c /tmp/s.jar -X POST localhost:8000/api/v1/auth/login -H 'content-type: application/json' \
     -d '{"email":"you@example.com","password":"YOUR-PASSWORD"}'
CSRF=$(awk '/sentinel_csrf/ {print $7}' /tmp/s.jar)

curl -b /tmp/s.jar -X POST localhost:8000/api/v1/integrations \
     -H 'content-type: application/json' -H "X-CSRF-Token: $CSRF" \
     -d '{"name":"my-app","kind":"otel_app","expected_signals":["metrics","logs","traces"]}'
```

The response contains `ingest_token` (`sit_…`). **It is shown once.** Point any OpenTelemetry SDK at the gateway:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4319
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20sit_..."
```

To onboard AVIRO: `cd ../aviro-shop && make up-sentinel TOKEN=sit_...`

## Manage it

| Action | Request |
|---|---|
| See health | `GET /api/v1/integrations/{id}/status` |
| Replace the token | `POST /api/v1/integrations/{id}/tokens/rotate` (the old token stops at once) |
| Revoke the token | `POST /api/v1/integrations/{id}/tokens/revoke` |
| Disable the integration | `DELETE /api/v1/integrations/{id}` (history is kept) |
| See what was discovered | `GET /api/v1/entities?type=service` and `/api/v1/entities/{id}/relations` |

## Health states

| Status | Meaning |
|---|---|
| `pending` | Created, nothing received yet |
| `healthy` | Every expected signal is arriving |
| `degraded` | An expected signal is missing, or the integration exceeds the metric series limit (5,000) |
| `stale` | Nothing received for 270 s. Reported within 5 minutes of the source stopping |
| `revoked` / `disabled` | Set by an administrator. Data is refused |

## Tests

```bash
make stack-up-fast    # short health intervals
make test-gateway     # end-to-end: auth, spoofing, normalisation, redaction, health, discovery
```
