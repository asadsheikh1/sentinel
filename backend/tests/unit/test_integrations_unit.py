"""Ingest tokens, health rules, entity parsing, limits and gateway config (no containers)."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from sentinel.db.models import EntityType, IntegrationStatus
from sentinel.integrations.entities import DiscoveredRelation, parse_resources
from sentinel.integrations.gate import PerMinuteLimiter
from sentinel.integrations.health import compute_health
from sentinel.integrations.service import config_hash
from sentinel.integrations.tokens import (
    bearer_token,
    hash_ingest_token,
    is_well_formed,
    new_ingest_token,
)

DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
STALE = timedelta(seconds=270)


def _ago(seconds: int) -> datetime:
    return NOW - timedelta(seconds=seconds)


# ------------------------------------------------------------------ tokens


def test_token_format_and_bearer_parsing() -> None:
    token = new_ingest_token()
    assert token.plaintext.startswith(f"sit_{token.prefix}_")
    assert is_well_formed(token.plaintext)
    assert bearer_token(f"Bearer {token.plaintext}") == token.plaintext
    assert bearer_token(f"bearer {token.plaintext}") == token.plaintext
    for bad in (None, "", token.plaintext, f"Basic {token.plaintext}", "Bearer nope"):
        assert bearer_token(bad) is None


def test_token_hash_is_keyed() -> None:
    token = new_ingest_token().plaintext
    assert hash_ingest_token("a" * 32, token) != hash_ingest_token("b" * 32, token)
    assert token not in hash_ingest_token("a" * 32, token)


def test_config_hash_ignores_key_order() -> None:
    assert config_hash({"a": 1, "b": 2}) == config_hash({"b": 2, "a": 1})
    assert config_hash({"a": 1}) != config_hash({"a": 2})


# ------------------------------------------------------------------ health


@pytest.mark.parametrize(
    ("expected", "seen", "status"),
    [
        ([], {}, IntegrationStatus.PENDING),
        (["metrics"], {}, IntegrationStatus.PENDING),
        ([], {"metrics": _ago(10)}, IntegrationStatus.HEALTHY),
        (["metrics", "logs"], {"metrics": _ago(5), "logs": _ago(5)}, IntegrationStatus.HEALTHY),
        (["metrics", "logs"], {"metrics": _ago(5)}, IntegrationStatus.DEGRADED),
        (["metrics", "logs"], {"metrics": _ago(5), "logs": _ago(900)}, IntegrationStatus.DEGRADED),
        (["metrics"], {"metrics": _ago(270)}, IntegrationStatus.STALE),
        ([], {"metrics": _ago(900), "logs": _ago(900)}, IntegrationStatus.STALE),
    ],
)
def test_health_rules(
    expected: list[str], seen: dict[str, datetime], status: IntegrationStatus
) -> None:
    health = compute_health(IntegrationStatus.PENDING, expected, seen, now=NOW, stale_after=STALE)
    assert health.status is status


@pytest.mark.parametrize("state", [IntegrationStatus.DISABLED, IntegrationStatus.REVOKED])
def test_disabled_and_revoked_never_change(state: IntegrationStatus) -> None:
    health = compute_health(state, [], {"metrics": _ago(1)}, now=NOW, stale_after=STALE)
    assert health.status is state


def test_too_many_series_is_degraded() -> None:
    health = compute_health(
        IntegrationStatus.HEALTHY,
        [],
        {"metrics": _ago(1)},
        now=NOW,
        stale_after=STALE,
        series_count=6000,
        max_series=5000,
    )
    assert health.status is IntegrationStatus.DEGRADED
    assert "6000" in str(health.reason)


def test_default_stale_timing_meets_the_five_minute_target() -> None:
    from sentinel.config import Settings

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.integration_stale_after_s + s.integration_flush_interval_s <= 300


# ---------------------------------------------------------------- rate limit


def test_per_minute_limiter() -> None:
    now = [120.0]
    limiter = PerMinuteLimiter(clock=lambda: now[0])
    key, other = uuid.uuid4(), uuid.uuid4()
    assert [limiter.hit(key, 2) for _ in range(2)] == [None, None]
    assert limiter.hit(key, 2) == 60
    assert limiter.hit(other, 2) is None  # tokens are independent
    now[0] = 180.0
    assert limiter.hit(key, 2) is None


# ------------------------------------------------------------------ entities


def test_service_container_host_topology() -> None:
    owner = uuid.uuid4()
    entities, relations = parse_resources(
        [
            {
                "service_name": "orders",
                "service_namespace": "aviro",
                "service_version": "1.2",
                "host_name": "host-a",
                "container_name": "orders-1",
                "deployment_environment_name": "prod",
                "sentinel_integration_id": str(owner),
            }
        ]
    )
    by_type = {e.type: e for e in entities}
    assert by_type[EntityType.SERVICE].key == "aviro/orders@prod"
    assert by_type[EntityType.HOST].key == "host-a"
    assert by_type[EntityType.CONTAINER].key == "host-a/orders-1"
    assert all(e.integration_ids == {owner} for e in entities)
    assert relations == {
        DiscoveredRelation(
            (EntityType.SERVICE, "aviro/orders@prod"), (EntityType.CONTAINER, "host-a/orders-1")
        ),
        DiscoveredRelation((EntityType.CONTAINER, "host-a/orders-1"), (EntityType.HOST, "host-a")),
    }


def test_service_on_host_without_container() -> None:
    _, relations = parse_resources([{"service_name": "api", "host_name": "h1"}])
    assert relations == {
        DiscoveredRelation((EntityType.SERVICE, "api@default"), (EntityType.HOST, "h1"))
    }


def test_unknown_service_and_bad_integration_id_are_tolerated() -> None:
    entities, relations = parse_resources(
        [{"service_name": "unknown_service:python", "sentinel_integration_id": "nope"}]
    )
    assert entities == []
    assert relations == set()


def test_same_entity_from_two_integrations_merges() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    entities, _ = parse_resources(
        [
            {"host_name": "h1", "sentinel_integration_id": str(a)},
            {"host_name": "h1", "sentinel_integration_id": str(b)},
        ]
    )
    assert len(entities) == 1
    assert entities[0].integration_ids == {a, b}


# ------------------------------------------------------- gateway safety rules


def _gateway() -> dict[str, Any]:
    return yaml.safe_load((DEPLOY / "otel" / "gateway.yaml").read_text())  # type: ignore[no-any-return]


def test_identity_is_deleted_before_being_set_from_the_token() -> None:
    actions = _gateway()["processors"]["resource/identity"]["attributes"]
    deleted = [a["key"] for a in actions[:2] if a["action"] == "delete"]
    assert deleted == ["sentinel.integration.id", "sentinel.integration.kind"]
    assert actions[2]["from_context"] == "metadata.x-sentinel-integration-id"
    assert actions[3]["from_context"] == "metadata.x-sentinel-integration-kind"


def test_every_input_pipeline_applies_identity_then_normalises_then_redacts() -> None:
    pipelines = _gateway()["service"]["pipelines"]
    for name in ("metrics", "logs", "traces"):
        processors = pipelines[name]["processors"]
        assert processors[:4] == [
            "memory_limiter",
            "resource/identity",
            "transform/normalise",
            "redaction",
        ]
        assert processors[-1] == "batch"


def test_caddy_strips_client_identity_and_the_token() -> None:
    caddyfile = (DEPLOY / "caddy" / "Caddyfile").read_text()
    order = [
        caddyfile.index("request_header -X-Sentinel-Integration-Id"),
        caddyfile.index("forward_auth api:8000"),
        caddyfile.index("request_header -Authorization"),
        caddyfile.index("reverse_proxy gateway:4318"),
    ]
    assert order == sorted(order)


def test_gateway_is_not_published() -> None:
    services = yaml.safe_load((DEPLOY / "compose" / "compose.observability.yaml").read_text())[
        "services"
    ]
    assert "ports" not in services["gateway"]
    assert services["caddy"]["ports"] == ["127.0.0.1:4319:4319"]
