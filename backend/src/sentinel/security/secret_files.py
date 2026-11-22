"""Secrets that the API provisions for its own containers (prober, Alertmanager).

Written atomically to a volume that only the API (write) and the one consumer
(read) mount. The consumer re-reads the file, so rotating a secret needs no
restart.
"""

import os
from pathlib import Path


def write_secret(path: Path, value: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value + "\n")
    os.chmod(tmp, mode)  # O_CREAT honours the umask; make the mode exact
    os.replace(tmp, path)  # atomic: a reader never sees half a secret


def read_secret(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""
