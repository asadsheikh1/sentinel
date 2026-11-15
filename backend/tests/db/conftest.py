"""Real-Postgres fixtures (Testcontainers, pgvector/pg17).

One container per test session. The setup mirrors production:

1. ``scripts/db_roles.sql`` runs as superuser (creates the three roles).
2. Alembic migrates as ``sentinel_migrator``.
3. Tests talk to the database as ``sentinel_app`` (the API's role), so
   privilege rules are exercised for real.

Each test gets a session inside a transaction that is rolled back afterwards.
"""

import shlex
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer

from sentinel.config import Settings
from sentinel.db import migrate

PGVECTOR_IMAGE = "docker.io/pgvector/pgvector:pg17"
ROLES_SQL = Path(__file__).resolve().parents[3] / "scripts" / "db_roles.sql"
SUPERUSER = "postgres"
SUPERUSER_PASSWORD = "test-superuser"
PASSWORDS = {
    "sentinel_migrator": "test-migrator",
    "sentinel_app": "test-app",
    "sentinel_readonly": "test-readonly",
}


@dataclass(frozen=True)
class Pg:
    container: PostgresContainer
    host: str
    port: int

    def url(self, role: str, database: str = "sentinel") -> str:
        password = SUPERUSER_PASSWORD if role == SUPERUSER else PASSWORDS[role]
        return f"postgresql+asyncpg://{role}:{password}@{self.host}:{self.port}/{database}"

    def psql(self, database: str, sql_file_contents: str, **variables: str) -> None:
        """Run a SQL script with psql *inside* the container (no host psql needed)."""
        var_args = " ".join(f"-v {k}={shlex.quote(v)}" for k, v in variables.items())
        script = (
            f"cat > /tmp/script.sql <<'__SQL__'\n{sql_file_contents}\n__SQL__\n"
            f"psql -v ON_ERROR_STOP=1 -U {SUPERUSER} -d {shlex.quote(database)} "
            f"{var_args} -f /tmp/script.sql"
        )
        result = self.container.exec(["bash", "-c", script])
        if result.exit_code != 0:
            raise RuntimeError(f"psql failed: {result.output.decode()}")

    def create_database(self, name: str) -> None:
        self.psql("postgres", f'CREATE DATABASE "{name}";')

    def apply_roles(self, database: str) -> None:
        self.psql(
            database,
            ROLES_SQL.read_text(),
            migrator_password=PASSWORDS["sentinel_migrator"],
            app_password=PASSWORDS["sentinel_app"],
            readonly_password=PASSWORDS["sentinel_readonly"],
        )


@pytest.fixture(scope="session")
def pg() -> Iterator[Pg]:
    container = PostgresContainer(
        PGVECTOR_IMAGE,
        username=SUPERUSER,
        password=SUPERUSER_PASSWORD,
        dbname="sentinel",
        driver=None,
    )
    with container:
        yield Pg(
            container=container,
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
        )


@pytest.fixture(scope="session")
def base_settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture(scope="session")
def migrated(pg: Pg, base_settings: Settings) -> Pg:
    """The ``sentinel`` database with roles applied and migrations at head."""
    pg.apply_roles("sentinel")
    migrate.upgrade(base_settings, "head", url=pg.url("sentinel_migrator"))
    return pg


@pytest.fixture
def fresh_database(pg: Pg) -> str:
    """An empty database (roles applied, no migrations) for migration tests."""
    name = f"t_{uuid.uuid4().hex[:12]}"
    pg.create_database(name)
    pg.apply_roles(name)
    return name


async def _engine(url: str) -> AsyncEngine:
    return create_async_engine(url, poolclass=NullPool)


@pytest.fixture(scope="session")
async def app_engine(migrated: Pg) -> AsyncIterator[AsyncEngine]:
    engine = await _engine(migrated.url("sentinel_app"))
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
async def migrator_engine(migrated: Pg) -> AsyncIterator[AsyncEngine]:
    engine = await _engine(migrated.url("sentinel_migrator"))
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
async def readonly_engine(migrated: Pg) -> AsyncIterator[AsyncEngine]:
    engine = await _engine(migrated.url("sentinel_readonly"))
    yield engine
    await engine.dispose()


@pytest.fixture
async def app_conn(app_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A connection as ``sentinel_app`` inside a transaction that is always rolled back."""
    async with app_engine.connect() as conn:
        trans = await conn.begin()
        try:
            yield conn
        finally:
            if trans.is_active:
                await trans.rollback()


@pytest.fixture
async def db_session(app_conn: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """ORM session bound to ``app_conn``; ``commit()`` only releases a savepoint."""
    session = AsyncSession(
        bind=app_conn, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    try:
        yield session
    finally:
        await session.close()
