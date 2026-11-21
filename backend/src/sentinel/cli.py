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


def _run_db(args: argparse.Namespace) -> int:
    import logging

    from sentinel.config import get_settings
    from sentinel.db import migrate

    # Alembic reports each applied migration through stdlib logging.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    settings = get_settings()
    match args.db_command:
        case "upgrade":
            migrate.upgrade(settings, args.revision)
        case "downgrade":
            migrate.downgrade(settings, args.revision)
        case "current":
            migrate.current(settings)
        case "revision":
            migrate.revision(settings, args.message)
        case "docs":
            from sentinel.db.docs import write_or_check

            return write_or_check(args.output, check=args.check)
    return 0


def _read_password(from_stdin: bool) -> str:
    import getpass
    import sys

    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password (min 12 chars): ")
    if getpass.getpass("Repeat password: ") != first:
        raise SystemExit("Passwords do not match.")
    return first


def _run_create_admin(args: argparse.Namespace) -> int:
    import asyncio

    from sentinel.auth.passwords import PasswordPolicyError
    from sentinel.auth.service import Actor, AuthService, ConflictError
    from sentinel.config import get_settings
    from sentinel.db.engine import create_engine, create_session_factory, session_scope
    from sentinel.db.models import UserRole

    settings = get_settings()
    password = _read_password(args.password_stdin)

    async def run() -> None:
        engine = create_engine(settings)
        try:
            async with session_scope(create_session_factory(engine)) as db:
                user, _ = await AuthService(db, settings).create_user(
                    email=args.email,
                    display_name=args.name,
                    role=UserRole.ADMIN,
                    password=password,
                    actor=Actor.system("cli:create-admin"),
                )
                print(f"Created admin {user.email} ({user.id}).")
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    except (ConflictError, PasswordPolicyError) as e:
        print(f"Error: {e}")
        return 1
    return 0


def _run_rbac_docs(args: argparse.Namespace) -> int:
    from sentinel.api.app import create_app
    from sentinel.api.rbac_docs import write_or_check
    from sentinel.config import Settings

    app = create_app(Settings(_env_file=None))
    return write_or_check(app, args.output, check=args.check)


def _run_telemetry(args: argparse.Namespace) -> int:
    from sentinel.config import get_settings
    from sentinel.telemetry import cli as telemetry_cli

    return telemetry_cli.run(args, get_settings())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sentinel", description="SENTINEL AI SRE")
    parser.add_argument("--version", action="version", version=f"sentinel {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    api = sub.add_parser("api", help="run the HTTP API server")
    api.add_argument("--host", default=None)
    api.add_argument("--port", type=int, default=None)
    api.add_argument("--reload", action="store_true", help="auto-reload (development)")
    api.set_defaults(func=_run_api)

    db = sub.add_parser("db", help="database migrations and docs")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    up = db_sub.add_parser("upgrade", help="apply migrations (default: to head)")
    up.add_argument("revision", nargs="?", default="head")
    down = db_sub.add_parser("downgrade", help="revert migrations (e.g. -1 or base)")
    down.add_argument("revision")
    db_sub.add_parser("current", help="show the applied revision")
    rev = db_sub.add_parser("revision", help="autogenerate a migration from model changes")
    rev.add_argument("-m", "--message", required=True)
    docs = db_sub.add_parser("docs", help="generate docs/database.md from the models")
    docs.add_argument("--output", default="../docs/database.md")
    docs.add_argument("--check", action="store_true", help="fail if the file is out of date")
    db.set_defaults(func=_run_db)

    admin = sub.add_parser("create-admin", help="create an admin user (the only way to bootstrap)")
    admin.add_argument("--email", required=True)
    admin.add_argument("--name", required=True, help="display name")
    admin.add_argument(
        "--password-stdin", action="store_true", help="read the password from stdin (scripts)"
    )
    admin.set_defaults(func=_run_create_admin)

    rbac = sub.add_parser("rbac-docs", help="generate docs/rbac.md from the code")
    rbac.add_argument("--output", default="../docs/rbac.md")
    rbac.add_argument("--check", action="store_true", help="fail if the file is out of date")
    rbac.set_defaults(func=_run_rbac_docs)

    from sentinel.telemetry import cli as telemetry_cli

    telemetry_cli.add_parser(sub)
    sub.choices["telemetry"].set_defaults(func=_run_telemetry)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
