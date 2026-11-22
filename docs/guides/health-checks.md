# External health checks

SENTINEL can check your public endpoints from the outside, with nothing to
install. It checks that they answer correctly and quickly, and that their TLS
certificates aren't about to expire. These checks catch the outages that
silence everything else: a host that is down sends no agent data at all.

## Kinds of check

| Kind | Target | Passes when |
|---|---|---|
| `http` | `https://shop.example.com/health` | The status is 200–399, or is listed in `expected_status`. Optionally the body contains a text (`body_contains`) or a JSON field has a value (`json_field`). Redirects are followed (at most 5). For `https`, the certificate is verified |
| `tcp` | `db.example.com:5432` | A TCP connection opens |
| `tls` | `shop.example.com` or `shop.example.com:8443` | The TLS handshake succeeds and the certificate is valid. Optionally it must stay valid for at least `min_days_valid` days |

Every check has:
- **`interval_s`:** 30 s or more (the minimum in production);
- **`timeout_s`:** up to 30 s, and shorter than the interval.

A check belongs to an integration of kind `http_check`, which groups checks for
example per application.

## Adding a check

```bash
# 1. An integration to group the checks (once)
curl -X POST http://127.0.0.1:8000/api/v1/integrations -b cookies.txt -H "X-CSRF-Token: $CSRF" \
  -H 'Content-Type: application/json' \
  -d '{"name": "shop public endpoints", "kind": "http_check", "expected_signals": ["metrics"]}'

# 2. The check
curl -X POST http://127.0.0.1:8000/api/v1/checks -b cookies.txt -H "X-CSRF-Token: $CSRF" \
  -H 'Content-Type: application/json' \
  -d '{"integration_id": "<id>", "name": "shop health", "kind": "http",
       "target": "https://shop.example.com/health", "interval_s": 60, "timeout_s": 10,
       "assertions": {"expected_status": [200], "json_field": {"path": "status", "equals": "ok"}}}'
```

`PATCH /api/v1/checks/{id}` changes a check, and `DELETE` removes it. Adding,
changing and deleting checks needs `integrations:manage`, and every change is
written to the audit log. You can have at most 50 checks.

## What you get

| Where | What |
|---|---|
| Prometheus | One series per check, labelled `sentinel_check_id`, `sentinel_check_name` and `server_address`. The metrics are below |
| Check state | `GET /api/v1/checks/{id}` → `state`: `unknown`, `up`, or `down` (**two failures in a row**). One failure alone doesn't flip the state |
| Events | Every state change is a `probe_state` event. `down` is `critical`, `up` is `info`. `GET /api/v1/events?type=probe_state` |
| Logs | Every failed run is logged in Loki, with its reason: `{service_name="sentinel-prober"}` |
| Inventory | Each check target is an `endpoint` entity |

The metrics:

| Metric | Value |
|---|---|
| `probe_success` | 1 or 0 |
| `probe_duration_seconds` | The whole check, DNS included |
| `probe_http_status_code` | The final response's status |
| `tls_cert_expiry_seconds` | When the certificate expires (Unix time) |

The failure reason (`timeout`, `connect: …`, `tls: …`, `status 500, expected
200-399`, `dns: …`, `blocked: …`) goes into the log, not into a metric label.

**How fast:** a failure is in Prometheus by the next run, within one interval. The state turns
`down` after the second failure. Measured with AVIRO at a 30 s interval: the
first failure arrived 26 s after the gateway stopped, and the state was `down`
after 57 s.

## Why some targets are refused

A health checker that users can point anywhere is a classic SSRF tool. For
example, it can be aimed at a cloud metadata service (`169.254.169.254`), at a
database inside the network, or at SENTINEL itself. The prober refuses anything
that isn't a public address, twice: when the check is saved, and before every
single connection.

| Refused | Examples |
|---|---|
| Private, loopback, link-local, CGNAT and reserved addresses, IPv4 and IPv6 | `10.0.0.5`, `127.0.0.1`, `169.254.169.254`, `100.64.0.1`, `[::1]`, `[fd00:ec2::254]` |
| IPv6 forms that embed one of those | `[::ffff:127.0.0.1]`, `[64:ff9b::a9fe:a9fe]` (NAT64), `[2002:7f00:1::]` (6to4) |
| Unusual ways of writing an IP | `2130706433`, `0177.0.0.1`, `0x7f.1`, `127.1` |
| Local and internal names | `localhost`, `*.internal`, `*.local`, `*.lan`; single-label names such as `postgres` or `api` |
| Names that resolve to any refused address | Even if one of several answers is public |
| URLs with credentials, other schemes | `http://user:pass@…`, `file://`, `gopher://` |
| Redirects to any of the above | Each redirect hop is checked again |

After the check, the connection goes to the IP that was validated, never to the
name again. This stops "DNS rebinding", where a name answers with a public IP
for the check and a private one for the connection. TLS is still verified
against the host name.

The prober also runs in its own container, on its own network. That network has
internet access and SENTINEL's ingest endpoint, and nothing else. The prober has
no database and no API access. It can only read its list of checks and send
results, using its own ingest token.

## Demo shop (development)

The demo shop runs on your machine, with private addresses only, so the guard
would refuse it. In the development stack, the prober allows exact host names
listed in `SENTINEL_PROBER_ALLOW_HOSTS` (default: `aviro_gateway_1` and the
test target). Production refuses to start with an allowlist set.

```bash
make stack-up
cd ../aviro-shop && make up-sentinel TOKEN=sit_...     # AVIRO joins the ingest network
# add a check for http://aviro_gateway_1:8000/healthz (as above), then:
podman stop aviro_gateway_1                              # watch it go down
podman start aviro_gateway_1                             # and back up
```
