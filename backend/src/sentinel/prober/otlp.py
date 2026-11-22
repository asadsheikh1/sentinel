"""Probe results as OTLP/HTTP JSON, sent through the gateway like any other source.

Metrics (Prometheus names after the gateway's translation):

| OTLP | Prometheus | Value |
|---|---|---|
| ``probe.success`` | ``probe_success`` | 1 or 0 |
| ``probe.duration`` (s) | ``probe_duration_seconds`` | whole check, DNS included |
| ``probe.http.status_code`` | ``probe_http_status_code`` | last response's status |
| ``tls.cert.expiry`` (s) | ``tls_cert_expiry_seconds`` | certificate notAfter, Unix time |

Each check is its own resource (``sentinel.check.*``, ``server.address``), so the
failure reason never becomes a label: it goes into a log record instead.
"""

from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

from sentinel.prober.checks import Result
from sentinel.prober.ssrf_guard import BlockedTargetError, split_host_port

SERVICE_NAME = "sentinel-prober"


def _attr(key: str, value: str | int) -> dict[str, Any]:
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": value}}


def _server(result: Result) -> tuple[str, int]:
    check = result.check
    if check.kind == "http":
        parts = urlsplit(check.target)
        default = 443 if parts.scheme == "https" else 80
        return parts.hostname or "", parts.port or default
    try:
        return split_host_port(check.target, default_port=443 if check.kind == "tls" else None)
    except BlockedTargetError:
        return check.target, 0


def resource(result: Result) -> dict[str, Any]:
    check = result.check
    host, port = _server(result)
    attrs = [
        _attr("service.name", SERVICE_NAME),
        _attr("sentinel.check.id", check.id),
        _attr("sentinel.check.name", check.name),
        _attr("sentinel.check.kind", check.kind),
        _attr("sentinel.check.integration_id", check.integration_id),
        _attr("server.address", host),
        _attr("server.port", port),
    ]
    return {"attributes": attrs}


def _gauge(name: str, unit: str, value: float | int, ts: int) -> dict[str, Any]:
    point: dict[str, Any] = {"timeUnixNano": str(ts)}
    if isinstance(value, int):
        point["asInt"] = str(value)
    else:
        point["asDouble"] = value
    return {"name": name, "unit": unit, "gauge": {"dataPoints": [point]}}


def metrics_payload(results: Iterable[Result]) -> dict[str, Any]:
    resource_metrics = []
    for r in results:
        ts = r.started_ns
        # unit "" (not "1"): Prometheus would otherwise name it probe_success_ratio
        metrics = [
            _gauge("probe.success", "", 1 if r.success else 0, ts),
            _gauge("probe.duration", "s", round(r.duration_s, 6), ts),
        ]
        if r.status_code is not None:
            metrics.append(_gauge("probe.http.status_code", "", r.status_code, ts))
        if r.cert_expiry_s is not None:
            metrics.append(_gauge("tls.cert.expiry", "s", float(r.cert_expiry_s), ts))
        resource_metrics.append(
            {
                "resource": resource(r),
                "scopeMetrics": [{"scope": {"name": "sentinel.prober"}, "metrics": metrics}],
            }
        )
    return {"resourceMetrics": resource_metrics}


def logs_payload(results: Iterable[Result]) -> dict[str, Any]:
    """One WARN record per failed run (successes are not logged)."""
    resource_logs = []
    for r in results:
        if r.success:
            continue
        attrs = [_attr("probe.error", r.error or "unknown")]
        if r.status_code is not None:
            attrs.append(_attr("http.response.status_code", r.status_code))
        record = {
            "timeUnixNano": str(r.started_ns),
            "severityNumber": 13,
            "severityText": "WARN",
            "body": {"stringValue": f"check {r.check.name!r} failed: {r.error}"},
            "attributes": attrs,
        }
        resource_logs.append(
            {
                "resource": resource(r),
                "scopeLogs": [{"scope": {"name": "sentinel.prober"}, "logRecords": [record]}],
            }
        )
    return {"resourceLogs": resource_logs}


def heartbeat_payload(checks: int) -> dict[str, Any]:
    """``sentinel_prober_checks``: how many checks the prober is running (also its pulse)."""
    import time

    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": [_attr("service.name", SERVICE_NAME)]},
                "scopeMetrics": [
                    {
                        "scope": {"name": "sentinel.prober"},
                        "metrics": [_gauge("sentinel.prober.checks", "", checks, time.time_ns())],
                    }
                ],
            }
        ]
    }
