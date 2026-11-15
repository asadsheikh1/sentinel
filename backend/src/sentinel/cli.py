"""Command-line entry point: ``sentinel <command>``."""

import argparse
from collections.abc import Sequence

from sentinel import __version__


def _run_api(args: argparse.Namespace) -> int:
    import uvicorn

    from sentinel.config import get_settings

    settings = get_settings()
    uvicorn.run(
        "sentinel.api.app:create_app",
        factory=True,
        host=args.host or settings.api_host,
        port=args.port or settings.api_port,
        reload=args.reload,
        log_config=None,
        access_log=False,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sentinel", description="SENTINEL AI SRE")
    parser.add_argument("--version", action="version", version=f"sentinel {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    api = sub.add_parser("api", help="run the HTTP API server")
    api.add_argument("--host", default=None)
    api.add_argument("--port", type=int, default=None)
    api.add_argument("--reload", action="store_true", help="auto-reload (development)")
    api.set_defaults(func=_run_api)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
