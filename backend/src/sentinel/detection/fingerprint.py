"""Error fingerprints: the same error with different IDs → the same fingerprint.

fingerprint = hash(exception type, normalised message, top in-app frame)

- **Normalised message:** IDs, numbers, addresses, timestamps and long quoted
  values become placeholders; short quoted identifiers (``'gift_message'``)
  stay, because they say *which* key or attribute.
- **Top in-app frame:** the innermost frame in the application's own code, as
  ``path:function`` with no line number (a redeploy that shifts lines is still
  the same error). The app's code is often installed in ``site-packages`` too,
  so "in-app" is: not the standard library, and either outside ``site-packages``
  or in one of the app's own packages (taken from where the error was logged).
- **No traceback** (a plain ERROR line): the log call site stands in for the frame.
"""

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sentinel.detection.traceback import Frame

VERSION = "v1"
MAX_MESSAGE_CHARS = 500

_SUBS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[a-z][a-z0-9+.-]*://[^\s'\"<>]+", re.I), "<url>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),  # noqa: E501
    (re.compile(r"""(?<![^\s'"<(\[])[^\s@'"<>()\[\],;]+@[\w-]+(?:\.[\w-]+)+"""), "<email>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),  # noqa: E501
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b", re.I), "<hex>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
    # Last: a quoted value that is (still) a plain identifier stays; anything else,
    # including one that now holds a placeholder, becomes <str>.
    (re.compile(r"""(['"])(?:(?!\1).){0,200}?\1""", re.S), "\0QUOTED\0"),
)  # fmt: skip
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_SPACE = re.compile(r"\s+")


def normalise_message(message: str) -> str:
    """Idempotent: normalising twice gives the same result."""
    text = message[: MAX_MESSAGE_CHARS * 4]
    for pattern, placeholder in _SUBS:
        if placeholder == "\0QUOTED\0":
            text = pattern.sub(_quoted, text)
        else:
            text = pattern.sub(placeholder, text)
    return _SPACE.sub(" ", text).strip()[:MAX_MESSAGE_CHARS]


def _quoted(match: re.Match[str]) -> str:
    inner = match.group(0)[1:-1]
    return match.group(0) if _IDENTIFIER.match(inner) else "<str>"


# ------------------------------------------------------------------ frames

_SITE = re.compile(r"/(?:site|dist)-packages/")
_STDLIB = re.compile(r"/lib/python\d+(?:\.\d+)?/(?!(?:site|dist)-packages/)")
_APP_ROOTS = re.compile(
    r"^(?:/usr/src/app|/app|/srv/[^/]+|/opt/[^/]+|/code|/workspace|/home/[^/]+)/"
)
# Frameworks and loggers that log on an app's behalf: never "the app's package".
_NOT_APP = frozenset(
    {
        "aiohttp", "asyncio", "celery", "concurrent", "django", "fastapi", "flask", "gunicorn",
        "logging", "loguru", "opentelemetry", "sentry_sdk", "starlette", "structlog", "uvicorn",
        "anyio", "httpx", "requests", "sqlalchemy", "urllib3",
    }
)  # fmt: skip


def normalise_path(path: str) -> str:
    """A path that's the same on every machine: ``aviro/orders/app.py``."""
    path = path.replace("\\", "/")
    if match := _SITE.search(path):
        return path[match.end() :]
    if match := _STDLIB.search(path):
        return "<stdlib>/" + path[match.end() :]
    return _APP_ROOTS.sub("", path, count=1).lstrip("/")


def package_of(path: str) -> str | None:
    """The top-level package of a file installed in site-packages."""
    if match := _SITE.search(path.replace("\\", "/")):
        top = path[match.end() :].split("/", 1)[0]
        return top.removesuffix(".py") or None
    return None


def app_packages(*, code_file_path: str | None, logger: str | None) -> frozenset[str]:
    """The app's own packages, from where it logged the error (call site, logger name)."""
    found = set()
    if code_file_path and (pkg := package_of(code_file_path)):
        found.add(pkg)
    if logger:
        found.add(logger.split(".", 1)[0])
    return frozenset(p for p in found if p and p not in _NOT_APP)


def is_in_app(frame: Frame, packages: Iterable[str]) -> bool:
    path = frame.file.replace("\\", "/")
    if path.startswith("<") or "/<frozen " in path or _STDLIB.search(path):
        return False
    if _SITE.search(path):
        return package_of(path) in set(packages)
    return True


def top_in_app_frame(frames: Sequence[Frame], packages: Iterable[str]) -> Frame | None:
    """The innermost in-app frame; else the innermost frame of all."""
    pkgs = frozenset(packages)
    for frame in reversed(frames):
        if is_in_app(frame, pkgs):
            return frame
    return frames[-1] if frames else None


def frame_key(frame: Frame) -> str:
    return (
        f"{normalise_path(frame.file)}:{frame.function}"
        if frame.function
        else normalise_path(frame.file)
    )


# ------------------------------------------------------------------ fingerprint


@dataclass(frozen=True)
class ErrorKey:
    exception_type: str  # "" for a plain ERROR line
    message: str  # normalised
    top_frame: str  # "" when nothing says where
    fingerprint: str


def fingerprint(exception_type: str | None, message: str, top_frame: str | None) -> ErrorKey:
    norm = normalise_message(message)
    etype = (exception_type or "").strip()[:200]
    where = (top_frame or "").strip()[:500]
    digest = hashlib.sha256("\0".join((VERSION, etype, norm, where)).encode()).hexdigest()[:32]
    return ErrorKey(etype, norm, where, digest)
