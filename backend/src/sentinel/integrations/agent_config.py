"""SENTINEL Agent configuration: validate an integration's options and render the
Collector config the installer downloads.

The agent is a pinned OpenTelemetry Collector (contrib) release (ADR-0013). It only
sends data (ADR-0016): nothing in the rendered config listens on a public
interface, and container-engine access goes through a GET-only socket proxy.

Safety of the rendering:

* options are validated with strict patterns before they are stored or rendered;
* every option value reaches the template through ``tojson``, so it is always a
  quoted scalar or list and can't add keys;
* the result is parsed again and checked, so a broken template fails here and not
  on a user's host.
"""

import hashlib
import re
import uuid
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from sentinel.db.models import IntegrationKind

AGENT_VERSION = "0.165.0"  # otelcol-contrib; keep in step with agent/install.sh
AGENT_KINDS = frozenset({IntegrationKind.LINUX_HOST, IntegrationKind.CONTAINER_HOST})
SELF_TELEMETRY_PORT = 18888
PROXY_SOCKET = "/run/sentinel-agent-proxy/engine.sock"
_TEMPLATE = "agent.yaml.j2"

# Absolute path, optional globs. No spaces, quotes, `..` or control characters.
_PATH = r"^/[A-Za-z0-9_.*?@+/-]{1,255}$"  # `..` is rejected separately
# Process executable names, systemd unit names, scrape job names.
_NAME = r"^[A-Za-z0-9_.@:+-]{1,64}$"
_JOB = r"^[A-Za-z0-9_-]{1,64}$"
_TARGET = r"^[A-Za-z0-9.-]{1,253}:[0-9]{1,5}$"
_METRICS_PATH = r"^/[A-Za-z0-9_./-]{0,127}$"
# Multiline start patterns (RE2 at the agent). A conservative character set.
_REGEX = r"^[A-Za-z0-9 _.,:;\-\[\]()^$\\*+?{}|/<>=#%&!@'\"]{1,200}$"

PathStr = Annotated[str, StringConstraints(pattern=_PATH)]
NameStr = Annotated[str, StringConstraints(pattern=_NAME)]


class AgentMode(StrEnum):
    HOST = "host"  # systemd service installed by agent/install.sh
    CONTAINER = "container"  # agent container with the host mounted at /hostfs


class LogFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: PathStr
    multiline_start: Annotated[str, StringConstraints(pattern=_REGEX)] | None = Field(
        default=None, description="A regex matching the first line of each record."
    )

    @field_validator("path")
    @classmethod
    def _no_parent(cls, value: str) -> str:
        if ".." in value:
            raise ValueError("path must not contain '..'")
        return value

    @field_validator("multiline_start")
    @classmethod
    def _compiles(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as e:
                raise ValueError(f"not a valid regular expression: {e}") from e
        return value


class PrometheusScrape(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job: Annotated[str, StringConstraints(pattern=_JOB)]
    target: Annotated[str, StringConstraints(pattern=_TARGET)]
    path: Annotated[str, StringConstraints(pattern=_METRICS_PATH)] = "/metrics"
    interval_s: int = Field(default=30, ge=10, le=300)

    @field_validator("target")
    @classmethod
    def _port(cls, value: str) -> str:
        port = int(value.rsplit(":", 1)[1])
        if not 1 <= port <= 65535:
            raise ValueError("port must be 1-65535")
        return value


class AgentOptions(BaseModel):
    """``integrations.config`` for ``linux_host`` and ``container_host``. No secrets here."""

    model_config = ConfigDict(extra="forbid")

    collection_interval_s: int = Field(default=30, ge=10, le=300)
    watched_processes: list[NameStr] = Field(default_factory=list, max_length=20)
    journald: bool = True
    journald_units: list[NameStr] = Field(default_factory=list, max_length=20)
    log_files: list[LogFile] = Field(default_factory=list, max_length=20)
    containers: bool | None = Field(
        default=None, description="Collect container stats. Default: on for container_host."
    )
    container_engine: Literal["podman", "docker"] = "podman"
    prometheus_scrapes: list[PrometheusScrape] = Field(default_factory=list, max_length=10)
    otlp_receiver: bool = True
    # Loopback ports for local apps. Change them if another Collector already uses them.
    otlp_grpc_port: int = Field(default=4317, ge=1024, le=65535)
    otlp_http_port: int = Field(default=4318, ge=1024, le=65535)

    @model_validator(mode="after")
    def _distinct_ports(self) -> "AgentOptions":
        ports = {self.otlp_grpc_port, self.otlp_http_port, SELF_TELEMETRY_PORT}
        if len(ports) != 3:
            raise ValueError(
                f"otlp_grpc_port, otlp_http_port and {SELF_TELEMETRY_PORT} must all differ"
            )
        return self


class AgentConfigError(ValueError):
    """The integration can't have an agent config, or its options are invalid."""


@dataclass(frozen=True)
class RenderedConfig:
    text: str
    config_hash: str
    options: AgentOptions


def parse_options(kind: IntegrationKind, config: dict[str, Any]) -> AgentOptions:
    if kind not in AGENT_KINDS:
        raise AgentConfigError(f"Integrations of kind {kind.value} don't use the agent.")
    return AgentOptions.model_validate(config)


def collects_containers(kind: IntegrationKind, options: AgentOptions) -> bool:
    if options.containers is None:
        return kind is IntegrationKind.CONTAINER_HOST
    return options.containers


def default_agent_dir() -> Path:
    """``agent/`` at the repository root (the API image copies it to /app/agent)."""
    return Path(__file__).resolve().parents[4] / "agent"


@lru_cache(maxsize=4)
def _environment(template_dir: str) -> SandboxedEnvironment:
    return SandboxedEnvironment(
        undefined=StrictUndefined,
        autoescape=False,  # YAML, not HTML; values are escaped with `tojson`
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
    )


def _load_template(agent_dir: Path) -> str:
    return (agent_dir / "templates" / _TEMPLATE).read_text(encoding="utf-8")


def _context(
    kind: IntegrationKind,
    environment: str,
    options: AgentOptions,
    mode: AgentMode,
    config_hash: str,
) -> dict[str, Any]:
    containers = collects_containers(kind, options)
    root = "/hostfs" if mode is AgentMode.CONTAINER else ""
    file_receivers = [f"file_log/{i}" for i in range(1, len(options.log_files) + 1)]
    metric_receivers = ["host_metrics"]
    if options.watched_processes:
        metric_receivers.append("host_metrics/processes")
    if containers:
        metric_receivers.append("docker_stats")
    metric_receivers.append("prometheus/self")
    if options.prometheus_scrapes:
        metric_receivers.append("prometheus/scrape")
    if options.otlp_receiver:
        metric_receivers.append("otlp")
    metric_processors = ["memory_limiter", "resource_detection", "resource/agent"]
    if containers:
        metric_processors.append("transform/containers")
    metric_processors.append("batch")
    log_receivers = file_receivers + (["otlp"] if options.otlp_receiver else [])
    return {
        "mode": mode.value,
        "root": root,
        "interval": f"{options.collection_interval_s}s",
        "environment": environment,
        "config_hash": config_hash,
        "agent_version": AGENT_VERSION,
        "storage_dir": "/var/lib/sentinel-agent",
        "watched_processes": options.watched_processes,
        "journald": options.journald,
        "journald_units": options.journald_units,
        "journal_dir": f"{root}/var/log/journal",
        "log_files": [
            {"path": f"{root}{f.path}", "multiline_start": f.multiline_start}
            for f in options.log_files
        ],
        "containers": containers,
        "engine_endpoint": f"unix://{PROXY_SOCKET}",
        "scrapes": [
            {"job": s.job, "target": s.target, "path": s.path, "interval": f"{s.interval_s}s"}
            for s in options.prometheus_scrapes
        ],
        "otlp": options.otlp_receiver,
        # Always loopback, also in the agent container: apps reach it only from
        # the same network namespace, so the agent never listens on a network.
        "otlp_host": "127.0.0.1",
        "otlp_grpc_port": options.otlp_grpc_port,
        "otlp_http_port": options.otlp_http_port,
        "self_port": SELF_TELEMETRY_PORT,
        "metric_receivers": metric_receivers,
        "metric_processors": metric_processors,
        "log_receivers": log_receivers,
    }


def _check(text: str) -> None:
    """The rendered file must be YAML with the expected shape."""
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict) or set(doc) != {
        "extensions",
        "receivers",
        "processors",
        "exporters",
        "service",
    }:
        raise AgentConfigError("Rendered agent config has an unexpected structure.")
    pipelines = doc["service"]["pipelines"]
    for name, pipeline in pipelines.items():
        for receiver in pipeline["receivers"]:
            if receiver not in doc["receivers"]:
                raise AgentConfigError(f"Pipeline {name} uses undefined receiver {receiver}.")
        for processor in pipeline["processors"]:
            if processor not in doc["processors"]:
                raise AgentConfigError(f"Pipeline {name} uses undefined processor {processor}.")
    if list(doc["exporters"]) != ["otlp_http/sentinel"]:
        raise AgentConfigError("The agent must export only to SENTINEL.")


def render(
    *,
    kind: IntegrationKind,
    environment: str,
    config: dict[str, Any],
    mode: AgentMode = AgentMode.HOST,
    agent_dir: Path | None = None,
) -> RenderedConfig:
    """Render the Collector config for one integration.

    The config hash covers the whole rendered file (computed with the hash left
    blank), so any change of options, template or Collector version changes it.
    The ingest endpoint and token are not part of the file: they come from the
    agent's environment file, so rotating a token doesn't change the hash.
    """
    options = parse_options(kind, config)
    directory = agent_dir or default_agent_dir()
    template = _environment(str(directory)).from_string(_load_template(directory))
    unhashed = template.render(_context(kind, environment, options, mode, ""))
    digest = hashlib.sha256(unhashed.encode()).hexdigest()[:16]
    text = template.render(_context(kind, environment, options, mode, digest))
    _check(text)
    return RenderedConfig(text=text, config_hash=digest, options=options)


def validate_options(kind: IntegrationKind, config: dict[str, Any]) -> None:
    """Reject invalid agent options when an integration is created or changed."""
    if kind in AGENT_KINDS:
        parse_options(kind, config)


def install_command(endpoint: str, integration_id: uuid.UUID) -> str:
    return (
        f"curl -fsSL {endpoint}/agent/install.sh -o sentinel-agent-install.sh && "
        f"sudo bash sentinel-agent-install.sh --endpoint {endpoint} "
        f"--integration {integration_id}"
    )


def container_command(endpoint: str, integration_id: uuid.UUID) -> str:
    """The same installer, running the agent as a container with the host at /hostfs."""
    return install_command(endpoint, integration_id) + " --mode container"
