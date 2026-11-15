from fastapi.testclient import TestClient

from sentinel import __version__
from sentinel.api.app import create_app
from sentinel.config import Environment, Settings


def test_healthz_returns_ok() -> None:
    client = TestClient(create_app(Settings(env=Environment.TEST)))
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__, "env": "test"}


def test_openapi_docs_enabled_outside_prod() -> None:
    client = TestClient(create_app(Settings(env=Environment.DEV)))
    assert client.get("/api/v1/openapi.json").status_code == 200


def test_openapi_docs_disabled_in_prod() -> None:
    client = TestClient(create_app(Settings(env=Environment.PROD)))
    assert client.get("/api/v1/openapi.json").status_code == 404
    assert client.get("/api/v1/docs").status_code == 404
