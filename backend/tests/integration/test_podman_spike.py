"""Podman compatibility spike (roadmap Step 1, risk R24).

Proves that Testcontainers can start a real pgvector Postgres through the
Podman socket. Later steps rely on this for the ``db`` and ``integration``
test suites.

Requires a container engine. Locally: ``make test-integration`` sets
``DOCKER_HOST`` to the Podman machine socket. In CI the GitHub ``ubuntu``
runner's rootless Podman socket is used.
"""

import psycopg
import pytest
from testcontainers.community.postgres import PostgresContainer

PGVECTOR_IMAGE = "docker.io/pgvector/pgvector:pg17"

pytestmark = pytest.mark.integration


def test_pgvector_container_starts_and_vector_extension_works() -> None:
    with (
        PostgresContainer(PGVECTOR_IMAGE, driver=None) as pg,
        psycopg.connect(pg.get_connection_url()) as conn,
    ):
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        row = conn.execute("SELECT '[1,2,3]'::vector <-> '[1,2,4]'::vector").fetchone()
        assert row is not None
        assert row[0] == pytest.approx(1.0)
        version = conn.execute("SHOW server_version").fetchone()
        assert version is not None
        assert str(version[0]).startswith("17")
