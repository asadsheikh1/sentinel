"""/readyz against a real database."""

import pytest
from fastapi.testclient import TestClient

from sentinel.api.app import create_app
from sentinel.config import Environment, Settings

from .conftest import Pg

pytestmark = pytest.mark.db


def test_readyz_ok_with_database(migrated: Pg) -> None:
    from pydantic import SecretStr

    settings = Settings(env=Environment.TEST, database_url=SecretStr(migrated.url("sentinel_app")))
    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok"}
