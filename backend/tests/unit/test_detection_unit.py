"""Pure detection logic: the telemetry-gap table, webhook signatures, input validation."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from sentinel.db.models import IntegrationKind, ProbeState
from sentinel.detection.gaps import classify
from sentinel.detection.signals import AlertmanagerWebhook
from sentinel.ingestion.deployments import DeploymentIn, dedupe_key
from sentinel.security.webhooks import sign, signature_valid

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "webhooks"
UP, DOWN, UNKNOWN = ProbeState.UP, ProbeState.DOWN, ProbeState.UNKNOWN


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


# ------------------------------------------------------------------ gap table (S15, S16)


@pytest.mark.parametrize(
    ("kind", "probes", "classification", "severity"),
    [
        # S15: agent stopped, host healthy, probe OK -> agent problem, low severity
        (IntegrationKind.CONTAINER_HOST, [UP], "agent_problem", "info"),
        (IntegrationKind.LINUX_HOST, [UP, UP], "agent_problem", "info"),
        # S16: agent silent and probe failing -> host or network down, high severity
        (IntegrationKind.LINUX_HOST, [DOWN], "host_or_network_down", "critical"),
        (IntegrationKind.LINUX_HOST, [UP, DOWN], "host_or_network_down", "critical"),
        # Nothing to tell the two apart
        (IntegrationKind.LINUX_HOST, [], "host_or_agent", "warning"),
        (IntegrationKind.LINUX_HOST, [UNKNOWN], "host_or_agent", "warning"),
        (IntegrationKind.LINUX_HOST, [UNKNOWN, UP], "agent_problem", "info"),
        # Not an agent: an app or a webhook source went quiet
        (IntegrationKind.OTEL_APP, [], "source_stopped", "warning"),
        (IntegrationKind.OTEL_APP, [DOWN], "source_stopped", "warning"),
        (IntegrationKind.HTTP_CHECK, [], "source_stopped", "warning"),
    ],
)
def test_gap_disambiguation_table(
    kind: IntegrationKind,
    probes: list[ProbeState],
    classification: str,
    severity: str,
) -> None:
    verdict = classify(kind, probes, "web-1")
    assert (verdict.classification, verdict.severity) == (classification, severity)
    assert verdict.summary.startswith("web-1")


# ------------------------------------------------------------------ signatures


def test_github_signature_is_checked_in_constant_time_and_exactly() -> None:
    body = b'{"zen": "Keep it logically awesome."}'
    good = sign("s3cret", body)
    assert good.startswith("sha256=")
    assert signature_valid("s3cret", body, good)
    assert not signature_valid("s3cret", body + b" ", good)  # body changed
    assert not signature_valid("other", body, good)  # wrong secret
    assert not signature_valid("s3cret", body, good.upper())
    assert not signature_valid("s3cret", body, good.removeprefix("sha256="))
    assert not signature_valid("s3cret", body, "sha1=" + good[7:])
    assert not signature_valid("s3cret", body, None)
    assert not signature_valid("", body, sign("", body))  # an empty secret never matches


# ------------------------------------------------------------------ Alertmanager payloads


def test_alertmanager_v4_payloads_parse() -> None:
    firing = AlertmanagerWebhook.model_validate(fixture("alertmanager_firing.json"))
    assert len(firing.alerts) == 2
    assert firing.alerts[0].labels["service_name"] == "orders"
    resolved = AlertmanagerWebhook.model_validate(fixture("alertmanager_resolved.json"))
    assert resolved.alerts[0].status == "resolved"


def _alert(**overrides: str) -> dict[str, Any]:
    alert = {"status": "firing", "labels": {}, "startsAt": "2026-11-22T09:00:00Z"}
    return {**alert, "fingerprint": "ab", **overrides}


@pytest.mark.parametrize(
    "change",
    [
        {"version": "3"},
        {"alerts": [_alert(status="pending")]},
        {"alerts": [_alert(fingerprint="XYZ")]},
    ],
)
def test_malformed_alertmanager_payloads_are_rejected(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        AlertmanagerWebhook.model_validate({**fixture("alertmanager_firing.json"), **change})


# ------------------------------------------------------------------ deployments


def test_deployment_input_is_validated() -> None:
    ok = DeploymentIn.model_validate(
        {
            "service": "orders",
            "version": "v1.4.0",
            "commit_sha": "2C6E7D8F9A0B",
            "deployed_at": "2026-11-22T09:00:00Z",
        }
    )
    assert ok.commit_sha == "2c6e7d8f9a0b"
    for bad in [
        {"service": ""},
        {"service": "orders; rm -rf /"},
        {"service": "orders", "commit_sha": "not-a-sha"},
        {"service": "orders", "deployed_at": "2026-11-22T09:00:00"},  # no timezone
        {"service": "orders", "status": "exploded"},
        {"service": "orders", "unknown": 1},
    ]:
        with pytest.raises(ValidationError):
            DeploymentIn.model_validate(bad)


def test_deployment_dedupe_key_prefers_the_idempotency_key() -> None:
    data = DeploymentIn(service="orders", version="v1")
    assert dedupe_key(data, "deploy-abc-orders") == "deployment:key:deploy-abc-orders"
    assert dedupe_key(data, None) == dedupe_key(DeploymentIn(service="orders", version="v1"), None)
    assert dedupe_key(data, None) != dedupe_key(DeploymentIn(service="orders", version="v2"), None)
