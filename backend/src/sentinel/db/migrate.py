"""Programmatic Alembic entry points (used by the CLI, `make migrate` and tests).

The database URL is passed through ``Config.attributes`` rather than the ini
file, so passwords never touch configparser interpolation or disk.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config

from sentinel.config import Settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["url"] = url
    return cfg


def _url(settings: Settings, url: str | None) -> str:
    return url or settings.effective_migration_url.get_secret_value()


def upgrade(settings: Settings, revision: str = "head", *, url: str | None = None) -> None:
    command.upgrade(alembic_config(_url(settings, url)), revision)


def downgrade(settings: Settings, revision: str, *, url: str | None = None) -> None:
    command.downgrade(alembic_config(_url(settings, url)), revision)


def current(settings: Settings, *, url: str | None = None) -> None:
    command.current(alembic_config(_url(settings, url)), verbose=True)


def revision(settings: Settings, message: str, *, url: str | None = None) -> None:
    """Autogenerate a new migration by diffing the models against the database."""
    command.revision(alembic_config(_url(settings, url)), message=message, autogenerate=True)
