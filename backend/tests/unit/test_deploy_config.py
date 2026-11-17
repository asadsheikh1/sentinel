"""Static checks on deploy/compose files (no containers needed)."""

import re
from pathlib import Path
from typing import Any

import yaml

DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
OBS = DEPLOY / "compose" / "compose.observability.yaml"
BUDGET_MIB = 2.5 * 1024


def _services() -> dict[str, Any]:
    return yaml.safe_load(OBS.read_text())["services"]  # type: ignore[no-any-return]


def _mib(limit: str) -> float:
    m = re.fullmatch(r"(\d+)([mg])", limit.lower())
    assert m, f"unparseable mem_limit {limit!r}"
    return int(m.group(1)) * (1024 if m.group(2) == "g" else 1)


def test_every_image_pinned_by_tag_and_digest() -> None:
    for name, svc in _services().items():
        assert re.fullmatch(r"docker\.io/[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}", svc["image"]), name


def test_memory_limits_within_budget() -> None:
    services = _services()
    assert all("mem_limit" in s for s in services.values())
    total = sum(_mib(s["mem_limit"]) for s in services.values())
    assert total <= BUDGET_MIB, f"{total} MiB > {BUDGET_MIB} MiB"


def test_ports_bound_to_localhost_only() -> None:
    for name, svc in _services().items():
        for port in svc.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name} publishes {port} publicly"


def test_retention_matches_plan() -> None:
    services = _services()
    assert "--storage.tsdb.retention.time=15d" in services["prometheus"]["command"]
    assert "--web.enable-otlp-receiver" in services["prometheus"]["command"]
    loki = yaml.safe_load((DEPLOY / "loki" / "loki.yaml").read_text())
    assert loki["limits_config"]["retention_period"] == "168h"
    assert loki["compactor"]["retention_enabled"] is True
    jaeger = yaml.safe_load((DEPLOY / "jaeger" / "config.yaml").read_text())
    badger = jaeger["extensions"]["jaeger_storage"]["backends"]["badger_main"]["badger"]
    assert badger["ttl"]["spans"] == "168h"
    assert badger["ephemeral"] is False


def test_prometheus_otlp_name_translation_is_pinned() -> None:
    prom = yaml.safe_load((DEPLOY / "prometheus" / "prometheus.yml").read_text())
    assert prom["otlp"]["translation_strategy"] == "UnderscoreEscapingWithSuffixes"
    assert "sentinel.integration.id" in prom["otlp"]["promote_resource_attributes"]
