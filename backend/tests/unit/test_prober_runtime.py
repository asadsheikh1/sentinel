"""Scheduler, OTLP encoding and the prober loop (against a fake SENTINEL)."""

import itertools
import json
import random
from pathlib import Path
from typing import Any

import httpx
import pytest

from sentinel.config import Settings
from sentinel.prober.checks import CheckSpec, Result
from sentinel.prober.otlp import logs_payload, metrics_payload
from sentinel.prober.run import Prober, TokenFile, parse_checks
from sentinel.prober.scheduler import FIRST_RUN_SPREAD_S, Schedule
from sentinel.prober.ssrf_guard import Guard


def spec(cid: str = "c1", interval: int = 30, **kw: Any) -> CheckSpec:
    return CheckSpec(
        id=cid,
        name=kw.get("name", cid),
        kind=kw.get("kind", "http"),
        target=kw.get("target", "https://shop.example.com/health"),
        interval_s=interval,
        timeout_s=5,
        integration_id="i1",
    )


# ------------------------------------------------------------------ scheduler


def _rng(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311 - deterministic jitter for tests


def test_new_checks_start_soon_and_spread_out() -> None:
    s = Schedule(_rng(1))
    s.update([spec(f"c{i}") for i in range(50)], now=100.0)
    starts = sorted(s.next_at.values())
    assert starts[0] >= 100.0
    assert starts[-1] <= 100.0 + FIRST_RUN_SPREAD_S
    assert starts[-1] - starts[0] > 0.8 * FIRST_RUN_SPREAD_S  # spread out, not all at once


def test_runs_follow_the_interval_with_small_jitter_and_no_drift() -> None:
    s = Schedule(_rng(2))
    s.update([spec(interval=30)], now=0.0)
    runs: list[float] = []
    t = 0.0
    while t < 30 * 20:
        if s.due(t):
            runs.append(t)
        t += 0.25
    gaps = [b - a for a, b in itertools.pairwise(runs)]
    assert len(runs) == 20
    assert all(28.0 <= g <= 32.0 for g in gaps), gaps  # ±5%
    assert abs((runs[-1] - runs[0]) / (len(runs) - 1) - 30) < 0.6  # no drift


def test_a_late_check_runs_once_not_in_a_burst() -> None:
    s = Schedule(_rng(3))
    s.update([spec(interval=30)], now=0.0)
    s.due(10.0)
    assert len(s.due(10_000.0)) == 1  # paused for hours: one run
    assert s.due(10_000.5) == []
    assert s.next_at["c1"] > 10_000.0 + 28


def test_updates_keep_unchanged_checks_and_drop_removed_ones() -> None:
    s = Schedule(_rng(4))
    s.update([spec("a"), spec("b")], now=0.0)
    kept = s.next_at["a"]
    s.update([spec("a"), spec("c")], now=50.0)
    assert s.next_at["a"] == kept
    assert "b" not in s.next_at
    assert s.next_at["c"] >= 50.0
    s.update([spec("a", interval=60)], now=60.0)  # changed: re-planned
    assert s.next_at["a"] >= 60.0
    assert s.sleep_for(60.0, maximum=0.5) <= 0.5
    assert Schedule().sleep_for(0.0) == 1.0


# ------------------------------------------------------------------ OTLP


def _result(success: bool, **kw: Any) -> Result:
    return Result(
        check=kw.pop("check", spec()), success=success, duration_s=0.25, started_ns=1_000, **kw
    )


def test_metrics_carry_the_check_identity_and_prometheus_friendly_units() -> None:
    payload = metrics_payload(
        [_result(True, status_code=200, cert_expiry_s=2_000_000_000.0), _result(False)]
    )
    first = payload["resourceMetrics"][0]
    attrs = {a["key"]: a["value"] for a in first["resource"]["attributes"]}
    assert attrs["service.name"] == {"stringValue": "sentinel-prober"}
    assert attrs["sentinel.check.id"] == {"stringValue": "c1"}
    assert attrs["sentinel.check.integration_id"] == {"stringValue": "i1"}
    assert attrs["server.address"] == {"stringValue": "shop.example.com"}
    assert attrs["server.port"] == {"intValue": "443"}
    metrics = {m["name"]: m for m in first["scopeMetrics"][0]["metrics"]}
    assert metrics["probe.success"]["unit"] == ""  # not "1": no _ratio suffix
    assert metrics["probe.success"]["gauge"]["dataPoints"][0]["asInt"] == "1"
    assert metrics["probe.duration"]["unit"] == "s"
    assert metrics["probe.http.status_code"]["gauge"]["dataPoints"][0]["asInt"] == "200"
    assert metrics["tls.cert.expiry"]["gauge"]["dataPoints"][0]["asDouble"] == 2e9
    second = payload["resourceMetrics"][1]["scopeMetrics"][0]["metrics"]
    assert {m["name"] for m in second} == {"probe.success", "probe.duration"}
    # The failure reason is never a metric label.
    assert "error" not in json.dumps(payload)


def test_failures_become_warn_logs_and_successes_are_not_logged() -> None:
    payload = logs_payload(
        [_result(True), _result(False, error="status 500, expected 200-399", status_code=500)]
    )
    assert len(payload["resourceLogs"]) == 1
    record = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert record["severityText"] == "WARN"
    assert "status 500" in record["body"]["stringValue"]


def test_tcp_targets_report_host_and_port() -> None:
    r = _result(False, check=spec(kind="tcp", target="db.example.com:5432"))
    attrs = {
        a["key"]: a["value"]
        for a in metrics_payload([r])["resourceMetrics"][0]["resource"]["attributes"]
    }
    assert attrs["server.address"] == {"stringValue": "db.example.com"}
    assert attrs["server.port"] == {"intValue": "5432"}


# ------------------------------------------------------------------ prober loop


class FakeSentinel:
    """The ingest endpoint as the prober sees it."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.metrics: list[dict[str, Any]] = []
        self.logs: list[dict[str, Any]] = []
        self.fail_exports = False
        self.checks = [
            {
                "id": "c1",
                "name": "shop",
                "kind": "http",
                "target": "http://shop.example.com/",
                "interval_s": 30,
                "timeout_s": 5,
                "integration_id": "i1",
                "assertions": {},
            }
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401)
        if request.url.path == "/api/v1/prober/checks":
            return httpx.Response(200, json={"version": "v1", "checks": self.checks})
        if self.fail_exports:
            return httpx.Response(503)
        body = json.loads(request.content)
        (self.metrics if request.url.path == "/v1/metrics" else self.logs).append(body)
        return httpx.Response(200, json={})


def _prober(tmp_path: Path, fake: FakeSentinel, token: str) -> Prober:
    token_file = tmp_path / "token"
    token_file.write_text(token + "\n")
    settings = Settings(_env_file=None, prober_token_file=str(token_file))  # type: ignore[call-arg]
    prober = Prober(settings, guard=Guard(resolver=_public))
    prober.sentinel = httpx.AsyncClient(
        base_url="http://ingest.sentinel.internal:4319", transport=httpx.MockTransport(fake)
    )
    return prober


async def _public(host: str, port: int) -> list[str]:
    return ["93.184.215.14"]


async def test_prober_loads_checks_and_exports_results(tmp_path: Path) -> None:
    fake = FakeSentinel("sit_good")
    prober = _prober(tmp_path, fake, "sit_good")
    assert await prober.refresh() == 1
    prober.results.append(_result(False, error="timeout"))
    assert await prober.flush()
    assert len(fake.metrics) == 1
    assert len(fake.logs) == 1


async def test_a_rotated_token_is_re_read_from_the_file(tmp_path: Path) -> None:
    fake = FakeSentinel("sit_new")
    prober = _prober(tmp_path, fake, "sit_old")
    (tmp_path / "token").write_text("sit_new\n")  # the API rotated it meanwhile
    assert await prober.refresh() == 1


async def test_results_are_kept_while_sentinel_is_unreachable(tmp_path: Path) -> None:
    fake = FakeSentinel("sit_good")
    prober = _prober(tmp_path, fake, "sit_good")
    fake.fail_exports = True
    prober.results.append(_result(True))
    assert not await prober.flush()
    assert len(prober.results) == 1
    fake.fail_exports = False
    assert await prober.flush()
    assert prober.results == []
    assert len(fake.metrics) == 1


async def test_missing_token_means_no_calls(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, prober_token_file=str(tmp_path / "absent"))  # type: ignore[call-arg]
    prober = Prober(settings)
    assert await prober.refresh() == -1
    assert TokenFile(None).get() is None


def test_the_allowlist_comes_from_the_prober_s_own_settings_only(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, prober_allow_hosts="aviro_gateway_1")  # type: ignore[call-arg]
    assert Prober(settings).guard.allow_hosts == frozenset({"aviro_gateway_1"})
    # A check list from the server can't carry an allowlist.
    checks = parse_checks({"checks": [], "allow_hosts": ["evil.internal"]})
    assert checks == []


@pytest.mark.parametrize("env", [{"prober_allow_hosts": "x"}, {"health_check_min_interval_s": 10}])
def test_development_escape_hatches_are_refused_in_prod(env: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"development only|>= 30"):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            env="prod",
            secret_key="x" * 40,
            internal_auth_secret="y" * 40,
            **env,
        )
