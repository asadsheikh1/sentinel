"""`sentinel telemetry …` against the recorded fixture (no backends needed)."""

import json
from pathlib import Path

import pytest

from sentinel.cli import main

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry"
REPLAY = ["--replay", str(FIXTURE / "aviro.json"), "--minutes", "20"]
TRACE = (FIXTURE / "trace_id.txt").read_text().strip()


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str]:
    code = main(["telemetry", *args])
    return code, capsys.readouterr().out


def test_metrics(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run(capsys, "metrics", "request_rate", "--namespace", "aviro", *REPLAY)
    assert code == 0
    assert 'service_namespace="aviro"' in out
    for service in ("gateway", "inventory", "orders", "payments"):
        assert f"{service}:" in out


def test_logs_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run(capsys, "logs", "--service", "fixture-secrets", "--limit", "20", *REPLAY)
    assert code == 0
    assert "Pr0d-Passw0rd!" not in out
    assert "[REDACTED:url_credentials]" in out
    assert "redacted:" in out


def test_error_logs(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run(capsys, "logs", "--service", "orders", "--errors", "--limit", "20", *REPLAY)
    assert code == 0
    assert "dependency call failed" in out


def test_traces_and_one_trace(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run(capsys, "traces", "--service", "orders", "--errors", "--limit", "3", *REPLAY)
    assert code == 0
    assert "ERROR" in out
    code, out = run(capsys, "trace", TRACE, "--replay", str(FIXTURE / "aviro.json"))
    assert code == 0
    assert "POST /api/orders" in out


def test_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run(
        capsys, "logs", "--service", "fixture-secrets", "--limit", "20", "--json", *REPLAY
    )
    assert code == 0
    data = json.loads(out)
    assert data["redactions"]["url_credentials"] == 1


def test_errors_are_reported_not_raised(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, out = run(capsys, "logs", "--service", 'x"}', *REPLAY)
    assert code == 1
    assert "not allowed" in out
    code, out = run(capsys, "traces", *REPLAY)
    assert code == 1
    assert "--service" in out
    code, out = run(
        capsys,
        "logs",
        "--service",
        "x",
        "--record",
        str(tmp_path / "a.json"),
        "--replay",
        str(tmp_path / "b.json"),
    )
    assert code == 1
    code, out = run(capsys, "trace", "0" * 32, "--replay", str(FIXTURE / "aviro.json"))
    assert code == 1  # not recorded in the fixture: a clean error, not a traceback
    assert "not recorded" in out
