"""Render the agent config for every kind x mode x option profile into a directory.

CI runs ``otelcol-contrib validate`` on each file (see .github/workflows/ci-agent.yml):

    uv run python tests/agent/render_matrix.py OUT_DIR
"""

import sys
from pathlib import Path
from typing import Any

from sentinel.db.models import IntegrationKind
from sentinel.integrations.agent_config import AGENT_KINDS, AgentMode, render

PROFILES: dict[str, dict[str, Any]] = {
    "defaults": {},
    "minimal": {"journald": False, "otlp_receiver": False},
    "full": {
        "collection_interval_s": 15,
        "watched_processes": ["sshd", "python3", "postgres"],
        "journald_units": ["ssh.service", "podman.service"],
        "log_files": [
            {"path": "/var/log/app/*.log", "multiline_start": r"^\d{4}-\d{2}-\d{2}"},
            {"path": "/var/log/nginx/error.log"},
        ],
        "containers": True,
        "prometheus_scrapes": [
            {"job": "celery", "target": "127.0.0.1:9808"},
            {"job": "postgres", "target": "localhost:9187", "path": "/metrics", "interval_s": 60},
        ],
    },
}


def main(out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    count = 0
    for kind in sorted(AGENT_KINDS, key=lambda k: k.value):
        for mode in AgentMode:
            for profile, config in PROFILES.items():
                rendered = render(
                    kind=IntegrationKind(kind), environment="ci", config=config, mode=mode
                )
                name = f"{kind.value}-{mode.value}-{profile}.yaml"
                (out / name).write_text(rendered.text)
                print(f"{name} {rendered.config_hash}")
                count += 1
    print(f"rendered {count} configs into {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
