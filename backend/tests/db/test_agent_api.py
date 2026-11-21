"""Agent endpoints: config download (ingest token), setup instructions, installer files."""

import uuid
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from sentinel.config import Settings
from sentinel.db.models import UserRole

from .authkit import app_with, client_for, login, make_settings, seed_user
from .conftest import Pg

pytestmark = pytest.mark.db


@pytest.fixture
def settings(migrated: Pg) -> Settings:
    return make_settings(
        migrated.url("sentinel_app"),
        ingest_auth_cache_ttl_s=0,
        agent_public_endpoint="https://ingest.example.com",
    )


def _create(client: TestClient, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    payload = {"name": f"agent-{uuid.uuid4().hex[:10]}", "kind": "container_host", **body}
    r = client.post("/api/v1/integrations", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


def _config(client: TestClient, iid: str, token: str | None, **params: str) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get(f"/api/v1/integrations/{iid}/agent-config", headers=headers, params=params)


def test_agent_downloads_its_rendered_config_with_its_ingest_token(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    with client_for(app_with(settings)) as client:
        a = login(client, admin)
        created = _create(client, a.apply(client), config={"watched_processes": ["sshd"]})
        iid, token = created["integration"]["id"], created["ingest_token"]
        client.cookies.clear()  # the agent has no session

        r = _config(client, iid, token)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("application/yaml")
        assert r.headers["cache-control"] == "no-store"
        assert token not in r.text  # the token lives only in the agent's env file
        d = yaml.safe_load(r.text)
        assert "docker_stats" in d["receivers"]
        assert d["receivers"]["host_metrics/processes"]["scrapers"]["process"]["include"][
            "names"
        ] == ["sshd"]
        attrs = {x["key"]: x["value"] for x in d["processors"]["resource/agent"]["attributes"]}
        assert attrs["sentinel.agent.config_hash"] == r.headers["x-sentinel-config-hash"]

        container = _config(client, iid, token, mode="container")
        assert container.status_code == 200
        assert yaml.safe_load(container.text)["receivers"]["host_metrics"]["root_path"] == "/hostfs"
        assert _config(client, iid, token, mode="other").status_code == 422


def test_config_needs_this_integrations_live_token(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    with client_for(app_with(settings)) as client:
        a = login(client, admin)
        h = a.apply(client)
        one = _create(client, h)
        other = _create(client, h)
        iid = one["integration"]["id"]

        assert _config(client, iid, None).status_code == 401
        assert _config(client, iid, "sit_aaaaaaaa_" + "b" * 43).status_code == 401
        # Another integration's valid token can't read this one's config.
        assert _config(client, iid, other["ingest_token"]).status_code == 401
        # A user session is not enough either: this route is for agents only.
        assert client.get(f"/api/v1/integrations/{iid}/agent-config").status_code == 401

        assert _config(client, iid, one["ingest_token"]).status_code == 200
        r = client.post(f"/api/v1/integrations/{iid}/tokens/revoke", headers=a.apply(client))
        assert r.status_code == 204
        assert _config(client, iid, one["ingest_token"]).status_code == 401


def test_non_agent_integrations_have_no_agent_config(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    with client_for(app_with(settings)) as client:
        a = login(client, admin)
        created = _create(client, a.apply(client), kind="otel_app")
        iid = created["integration"]["id"]
        assert _config(client, iid, created["ingest_token"]).status_code == 404
        assert client.get(f"/api/v1/integrations/{iid}/setup").status_code == 404


def test_invalid_agent_options_are_rejected_on_create_and_update(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    with client_for(app_with(settings)) as client:
        a = login(client, admin)
        bad = {"log_files": [{"path": "/var/log/x.log\nexporters: {}"}]}
        r = client.post(
            "/api/v1/integrations",
            json={"name": "bad-agent", "kind": "linux_host", "config": bad},
            headers=a.apply(client),
        )
        assert r.status_code == 422
        assert "log_files.0.path" in r.json()["detail"]

        iid = _create(client, a.apply(client), kind="linux_host")["integration"]["id"]
        r = client.patch(
            f"/api/v1/integrations/{iid}",
            json={"config": {"no_such_option": 1}},
            headers=a.apply(client),
        )
        assert r.status_code == 422
        r = client.patch(
            f"/api/v1/integrations/{iid}",
            json={"config": {"watched_processes": ["nginx"]}},
            headers=a.apply(client),
        )
        assert r.status_code == 200
        # Other kinds keep free-form config.
        _create(client, a.apply(client), kind="otel_app", config={"anything": [1, 2]})


def test_setup_gives_install_commands_without_the_token(settings: Settings) -> None:
    admin = seed_user(settings, role=UserRole.ADMIN)
    viewer = seed_user(settings, role=UserRole.VIEWER)
    with client_for(app_with(settings)) as client:
        a = login(client, admin)
        created = _create(client, a.apply(client))
        iid = created["integration"]["id"]
        r = client.get(f"/api/v1/integrations/{iid}/setup")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["endpoint"] == "https://ingest.example.com"
        assert body["install_command"] == (
            "curl -fsSL https://ingest.example.com/agent/install.sh -o sentinel-agent-install.sh"
            " && sudo bash sentinel-agent-install.sh --endpoint https://ingest.example.com"
            f" --integration {iid}"
        )
        assert body["container_command"].endswith("--mode container")
        assert body["agent_version"] == "0.165.0"
        assert created["ingest_token"] not in r.text
        assert any("socket proxy" in n for n in body["notes"])

        client.cookies.clear()
        login(client, viewer)
        assert client.get(f"/api/v1/integrations/{iid}/setup").status_code == 403


def test_installer_files_are_public_and_allowlisted(settings: Settings) -> None:
    with client_for(app_with(settings)) as client:
        for name in ("install.sh", "uninstall.sh", "socket-proxy.cfg", "Containerfile"):
            r = client.get(f"/agent/{name}")
            assert r.status_code == 200, name
            assert len(r.text) > 100
        assert client.get("/agent/install.sh").text.startswith("#!/usr/bin/env bash")
        for name in ("templates", "README.md", "..%2Fbackend%2Fpyproject.toml", "nope"):
            assert client.get(f"/agent/{name}").status_code == 404, name
