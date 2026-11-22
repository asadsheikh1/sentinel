"""Static checks on the Prometheus rule packs (promtool runs the behaviour tests)."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

PROM = Path(__file__).resolve().parents[3] / "deploy" / "prometheus"
PACKS = sorted((PROM / "rules" / "packs").glob("*.yml"))
TESTS = PROM / "tests" / "packs"
MAKEFILE = (PROM.parents[1] / "Makefile").read_text()
ENTITY_TYPES = {"host", "container", "service", "database", "endpoint", "integration"}


def _rules(path: Path) -> list[dict[str, Any]]:
    return [rule for group in yaml.safe_load(path.read_text())["groups"] for rule in group["rules"]]


def _alerts() -> list[tuple[Path, dict[str, Any]]]:
    return [(p, r) for p in PACKS for r in _rules(p) if "alert" in r]


def test_there_is_a_pack_per_entity_type() -> None:
    assert {p.stem for p in PACKS} == ENTITY_TYPES


@pytest.mark.parametrize(("path", "rule"), _alerts(), ids=[r["alert"] for _, r in _alerts()])
def test_every_alert_is_labelled_and_described(path: Path, rule: dict[str, Any]) -> None:
    labels = rule["labels"]
    assert labels["severity"] in {"critical", "warning", "info"}
    assert labels["pack"] == path.stem
    assert labels["entity_type"] in ENTITY_TYPES
    assert rule["annotations"]["summary"]


def test_every_alert_has_a_promtool_test() -> None:
    tested: set[str] = set()
    for test_file in TESTS.glob("*_test.yml"):
        doc = yaml.safe_load(test_file.read_text())
        for case in doc["tests"]:
            for check in case.get("alert_rule_test", []):
                if check.get("exp_alerts"):  # a test that the alert actually fires
                    tested.add(check["alertname"])
    alerts = {rule["alert"] for _, rule in _alerts()}
    assert alerts - tested == set(), "alerts without a firing test"


def test_every_pack_and_test_file_is_run_by_make_test_rules() -> None:
    for pack in PACKS:
        assert f"/p/rules/packs/{pack.name}" in MAKEFILE, pack.name
        assert f"{pack.stem}_test.yml" in MAKEFILE, pack.name
        assert (TESTS / f"{pack.stem}_test.yml").exists()


def test_alerts_group_by_identity_labels() -> None:
    """An aggregation that drops the entity's identity can't be attributed to it."""
    identity = {
        "host": "host_name",
        "container": "container_name",
        "service": "service_name",
        "database": "host_name",
        "endpoint": "sentinel_check_id",
        "integration": "sentinel_integration_id",
    }
    recorded = {
        r["record"]: r["expr"]
        for path in [*PACKS, PROM / "rules" / "red.yml"]
        for r in _rules(path)
        if "record" in r
    }

    def expand(expr: str, depth: int = 0) -> str:
        for name, body in recorded.items():
            if name in expr and depth < 5:
                expr += " " + expand(body, depth + 1)
        return expr

    for _, rule in _alerts():
        expr = re.sub(r"\s+", " ", expand(rule["expr"]))
        assert identity[rule["labels"]["entity_type"]] in expr, rule["alert"]


def test_prometheus_loads_the_packs() -> None:
    config = yaml.safe_load((PROM / "prometheus.yml").read_text())
    assert "/etc/prometheus/rules/packs/*.yml" in config["rule_files"]
