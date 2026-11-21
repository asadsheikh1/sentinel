"""SENTINEL Agent config rendering: options validation, safety, structure, hash."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from sentinel.db.models import IntegrationKind
from sentinel.integrations import agent_config
from sentinel.integrations.agent_config import (
    PROXY_SOCKET,
    AgentConfigError,
    AgentMode,
    render,
    validate_options,
)

HOST = IntegrationKind.LINUX_HOST
CONTAINERS = IntegrationKind.CONTAINER_HOST

FULL: dict[str, Any] = {
    "watched_processes": ["sshd", "python3"],
    "journald_units": ["ssh.service"],
    "log_files": [{"path": "/var/log/app/*.log", "multiline_start": r"^\d{4}-\d{2}-\d{2}"}],
    "prometheus_scrapes": [{"job": "celery", "target": "127.0.0.1:9808"}],
}


def doc(kind: IntegrationKind = HOST, config: dict[str, Any] | None = None, **kw: Any) -> Any:
    return yaml.safe_load(render(kind=kind, environment="dev", config=config or {}, **kw).text)


# ------------------------------------------------------------------ structure


def test_linux_host_defaults_collect_host_metrics_and_journal_but_not_containers() -> None:
    d = doc()
    assert set(d["receivers"]) == {"host_metrics", "journald", "prometheus/self", "otlp"}
    scrapers = d["receivers"]["host_metrics"]["scrapers"]
    assert {"cpu", "memory", "filesystem", "load", "disk", "network"} <= set(scrapers)
    assert "process" not in scrapers  # never every process
    assert "docker_stats" not in d["receivers"]
    assert set(d["service"]["pipelines"]) == {"metrics", "logs/journald", "logs", "traces"}


def test_container_host_collects_container_stats_through_the_proxy_only() -> None:
    d = doc(CONTAINERS)
    stats = d["receivers"]["docker_stats"]
    assert stats["endpoint"] == f"unix://{PROXY_SOCKET}"
    assert "docker_stats" in d["service"]["pipelines"]["metrics"]["receivers"]
    assert "transform/containers" in d["service"]["pipelines"]["metrics"]["processors"]
    # Never the raw engine sockets.
    text = render(kind=CONTAINERS, environment="dev", config={}).text
    assert "podman.sock" not in text
    assert "docker.sock" not in text


def test_containers_option_overrides_the_kind_default() -> None:
    assert "docker_stats" in doc(HOST, {"containers": True})["receivers"]
    assert "docker_stats" not in doc(CONTAINERS, {"containers": False})["receivers"]


def test_watched_processes_get_a_strict_process_scraper() -> None:
    d = doc(HOST, FULL)
    proc = d["receivers"]["host_metrics/processes"]["scrapers"]["process"]
    assert proc["include"] == {"names": ["sshd", "python3"], "match_type": "strict"}
    assert "host_metrics/processes" in d["service"]["pipelines"]["metrics"]["receivers"]


def test_log_files_scrape_and_journal_units() -> None:
    d = doc(HOST, FULL)
    f = d["receivers"]["file_log/1"]
    assert f["include"] == ["/var/log/app/*.log"]
    assert f["multiline"] == {"line_start_pattern": r"^\d{4}-\d{2}-\d{2}"}
    assert f["storage"] == "file_storage"  # offsets survive restarts
    assert d["receivers"]["journald"]["units"] == ["ssh.service"]
    assert d["receivers"]["prometheus/scrape"]["config"]["scrape_configs"][0]["static_configs"] == [
        {"targets": ["127.0.0.1:9808"]}
    ]
    assert d["service"]["pipelines"]["logs"]["receivers"] == ["file_log/1", "otlp"]


def test_container_mode_reads_the_host_through_hostfs() -> None:
    d = doc(HOST, FULL, mode=AgentMode.CONTAINER)
    assert d["receivers"]["host_metrics"]["root_path"] == "/hostfs"
    assert d["receivers"]["host_metrics/processes"]["root_path"] == "/hostfs"
    assert d["receivers"]["journald"]["directory"] == "/hostfs/var/log/journal"
    assert d["receivers"]["file_log/1"]["include"] == ["/hostfs/var/log/app/*.log"]
    assert "root_path" not in doc(HOST, FULL)["receivers"]["host_metrics"]


def test_everything_can_be_switched_off_except_host_metrics_and_self_telemetry() -> None:
    d = doc(HOST, {"journald": False, "otlp_receiver": False})
    assert set(d["receivers"]) == {"host_metrics", "prometheus/self"}
    assert set(d["service"]["pipelines"]) == {"metrics"}


def test_agent_exports_only_to_sentinel_with_secrets_from_the_environment() -> None:
    d = doc(CONTAINERS, FULL)
    assert list(d["exporters"]) == ["otlp_http/sentinel"]
    exporter = d["exporters"]["otlp_http/sentinel"]
    assert exporter["endpoint"] == "${env:SENTINEL_INGEST_ENDPOINT}"
    assert exporter["headers"] == {"Authorization": "Bearer ${env:SENTINEL_INGEST_TOKEN}"}
    assert exporter["sending_queue"]["storage"] == "file_storage"
    for pipeline in d["service"]["pipelines"].values():
        assert pipeline["exporters"] == ["otlp_http/sentinel"]


@pytest.mark.parametrize("mode", list(AgentMode))
def test_nothing_listens_beyond_loopback(mode: AgentMode) -> None:
    """ADR-0016: no inbound ports. Every listener is on 127.0.0.1."""
    d = doc(CONTAINERS, FULL, mode=mode)
    otlp = d["receivers"]["otlp"]["protocols"]
    assert otlp["grpc"]["endpoint"] == "127.0.0.1:4317"
    assert otlp["http"]["endpoint"] == "127.0.0.1:4318"
    reader = d["service"]["telemetry"]["metrics"]["readers"][0]["pull"]["exporter"]["prometheus"]
    assert reader["host"] == "127.0.0.1"
    text = render(kind=CONTAINERS, environment="dev", config=FULL, mode=mode).text
    assert "0.0.0.0" not in text  # noqa: S104


def test_self_telemetry_is_scraped_and_sent() -> None:
    d = doc()
    job = d["receivers"]["prometheus/self"]["config"]["scrape_configs"][0]
    assert job["job_name"] == "sentinel-agent"
    assert job["static_configs"] == [{"targets": [f"127.0.0.1:{agent_config.SELF_TELEMETRY_PORT}"]}]
    assert "prometheus/self" in d["service"]["pipelines"]["metrics"]["receivers"]


def test_resource_attributes_carry_the_config_hash_and_version() -> None:
    rendered = render(kind=HOST, environment="staging", config={})
    attrs = {
        a["key"]: a
        for a in yaml.safe_load(rendered.text)["processors"]["resource/agent"]["attributes"]
    }
    assert attrs["sentinel.agent.config_hash"]["value"] == rendered.config_hash
    assert attrs["sentinel.agent.version"]["value"] == agent_config.AGENT_VERSION
    # The environment is a default only: an app's own value wins.
    assert attrs["deployment.environment.name"] == {
        "key": "deployment.environment.name",
        "value": "staging",
        "action": "insert",
    }
    assert re.fullmatch(r"[0-9a-f]{16}", rendered.config_hash)


# ------------------------------------------------------------------ hash


def test_hash_is_stable_and_changes_with_anything_that_changes_the_file() -> None:
    def h(kind: IntegrationKind = HOST, env: str = "dev", **config: Any) -> str:
        return render(kind=kind, environment=env, config=config).config_hash

    assert h() == h()
    assert h() != h(watched_processes=["sshd"])
    assert h() != h(env="prod")
    assert h() != h(CONTAINERS)
    assert (
        h() != render(kind=HOST, environment="dev", config={}, mode=AgentMode.CONTAINER).config_hash
    )


def test_hash_covers_the_template(tmp_path: Path) -> None:
    original = agent_config.default_agent_dir() / "templates" / "agent.yaml.j2"
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "agent.yaml.j2").write_text(
        original.read_text().replace("timeout: 5s", "timeout: 6s")
    )
    changed = render(kind=HOST, environment="dev", config={}, agent_dir=tmp_path)
    assert changed.config_hash != render(kind=HOST, environment="dev", config={}).config_hash


# ------------------------------------------------------------------ validation


def test_only_agent_kinds_have_an_agent_config() -> None:
    with pytest.raises(AgentConfigError):
        render(kind=IntegrationKind.OTEL_APP, environment="dev", config={})
    validate_options(IntegrationKind.OTEL_APP, {"anything": "goes"})  # not ours to check


@pytest.mark.parametrize(
    "config",
    [
        {"unknown_option": True},
        {"log_files": [{"path": "relative/path.log"}]},
        {"log_files": [{"path": "/var/log/../../etc/shadow"}]},
        {"log_files": [{"path": '/var/log/x.log"\n  evil: true'}]},
        {"log_files": [{"path": "/var/log/a b.log"}]},
        {"log_files": [{"path": "/var/log/x.log", "multiline_start": "(unclosed"}]},
        {"watched_processes": ["sshd; rm -rf /"]},
        {"watched_processes": ["x" * 65]},
        {"watched_processes": [f"p{i}" for i in range(21)]},
        {"journald_units": ["ssh service"]},
        {"prometheus_scrapes": [{"job": "j", "target": "http://127.0.0.1:9100"}]},
        {"prometheus_scrapes": [{"job": "j", "target": "127.0.0.1:70000"}]},
        {"prometheus_scrapes": [{"job": "j", "target": "h:1", "path": "/m?x=1"}]},
        {"collection_interval_s": 1},
        {"container_engine": "containerd"},
    ],
)
def test_invalid_options_are_rejected(config: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        validate_options(HOST, config)


def test_a_hostile_environment_name_stays_a_quoted_value() -> None:
    """The environment comes from the integration, not from AgentOptions."""
    hostile = 'dev"\nexporters: {debug: {}}\n#'
    d = yaml.safe_load(render(kind=HOST, environment=hostile, config={}).text)
    assert list(d["exporters"]) == ["otlp_http/sentinel"]
    attrs = {a["key"]: a["value"] for a in d["processors"]["resource/agent"]["attributes"]}
    assert attrs["deployment.environment.name"] == hostile


_PATH_CHARS = st.text(
    alphabet=st.sampled_from("abcXYZ019_.-*?@+/ \"'\n:{}[]#&!|>"), min_size=1, max_size=40
)


@settings(max_examples=150, deadline=None)
@given(suffix=_PATH_CHARS, process=_PATH_CHARS)
def test_any_accepted_value_round_trips_and_never_changes_the_structure(
    suffix: str, process: str
) -> None:
    config = {"log_files": [{"path": "/" + suffix}], "watched_processes": [process]}
    try:
        validate_options(HOST, config)
    except ValidationError:
        return  # rejected up front: fine
    d = doc(HOST, config)
    assert d["receivers"]["file_log/1"]["include"] == ["/" + suffix]
    assert d["receivers"]["host_metrics/processes"]["scrapers"]["process"]["include"]["names"] == [
        process
    ]
    assert set(d) == {"extensions", "receivers", "processors", "exporters", "service"}


# ------------------------------------------------------------------ output check


def test_a_broken_template_fails_at_render_time(tmp_path: Path) -> None:
    original = agent_config.default_agent_dir() / "templates" / "agent.yaml.j2"
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "agent.yaml.j2").write_text(
        original.read_text().replace("  memory_limiter:\n", "  memory_limiter_typo:\n", 1)
    )
    with pytest.raises(AgentConfigError, match="undefined processor memory_limiter"):
        render(kind=HOST, environment="dev", config={}, agent_dir=tmp_path)


def test_a_template_adding_an_exporter_is_refused(tmp_path: Path) -> None:
    original = agent_config.default_agent_dir() / "templates" / "agent.yaml.j2"
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "agent.yaml.j2").write_text(
        original.read_text().replace("exporters:\n", "exporters:\n  debug: {}\n", 1)
    )
    with pytest.raises(AgentConfigError, match="only to SENTINEL"):
        render(kind=HOST, environment="dev", config={}, agent_dir=tmp_path)


def test_install_and_container_commands_never_contain_a_token() -> None:
    import uuid

    iid = uuid.uuid4()
    cmd = agent_config.install_command("https://ingest.example.com", iid)
    assert f"--integration {iid}" in cmd
    assert "sit_" not in cmd
    assert agent_config.container_command("https://ingest.example.com", iid).endswith(
        "--mode container"
    )


@pytest.mark.parametrize(
    ("mount", "excluded"),
    [
        ("/", False),
        ("/boot", False),
        ("/data/devices", False),  # an unanchored "/dev/*" would hide this
        ("/srv/runner", False),
        ("/dev/shm", True),  # noqa: S108 (a mount point, not a temp file)
        ("/run/user/1000", True),
        ("/proc", True),
        ("/var/lib/containers/storage/overlay/abc/merged", True),
        ("/var/lib/docker/overlay2/abc/merged", True),
        ("/home/core/.local/share/containers/storage/overlay/abc/merged", True),
        ("/snap/core22/1380", True),
    ],
)
def test_filesystem_exclusions_hide_pseudo_and_container_mounts_only(
    mount: str, excluded: bool
) -> None:
    fs = doc()["receivers"]["host_metrics"]["scrapers"]["filesystem"]
    patterns = fs["exclude_mount_points"]["mount_points"]
    assert fs["exclude_mount_points"]["match_type"] == "regexp"
    assert any(re.search(p, mount) for p in patterns) is excluded
    assert "overlay" not in fs["exclude_fs_types"]["fs_types"]


def test_otlp_ports_can_be_moved_when_another_collector_has_them() -> None:
    otlp = doc(HOST, {"otlp_grpc_port": 14317, "otlp_http_port": 14318})["receivers"]["otlp"]
    assert otlp["protocols"]["grpc"]["endpoint"] == "127.0.0.1:14317"
    assert otlp["protocols"]["http"]["endpoint"] == "127.0.0.1:14318"


@pytest.mark.parametrize(
    "config",
    [
        {"otlp_grpc_port": 4318},  # same as the HTTP default
        {"otlp_http_port": agent_config.SELF_TELEMETRY_PORT},
        {"otlp_grpc_port": 80},  # privileged
        {"otlp_http_port": 70000},
    ],
)
def test_invalid_or_clashing_ports_are_rejected(config: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        validate_options(HOST, config)
