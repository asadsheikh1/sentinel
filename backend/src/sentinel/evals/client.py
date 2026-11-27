"""SENTINEL's HTTP API, as the evaluation harness uses it: a client like any other.

The harness never reads SENTINEL's database or calls its code: what it measures
is what a user of the API would see.
"""

import time
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import httpx


class SentinelError(RuntimeError):
    pass


class SentinelClient:
    def __init__(self, base_url: str, email: str, password: str, *, timeout_s: float = 20) -> None:
        self._email = email
        self._password = password
        self._http = httpx.Client(base_url=base_url, timeout=timeout_s, trust_env=False)
        self._csrf = ""
        self.login()

    def close(self) -> None:
        self._http.close()

    def login(self) -> None:
        r = self._http.post(
            "/api/v1/auth/login", json={"email": self._email, "password": self._password}
        )
        if r.status_code != 200:
            raise SentinelError(f"login failed: HTTP {r.status_code}")
        self._csrf = r.json()["csrf_token"]
        # SENTINEL commits a request's transaction after the response is sent (Step 16
        # finding), so a request right after login can miss the new session. Wait for it.
        for _ in range(20):
            if self._http.get("/api/v1/auth/me").status_code == 200:
                return
            time.sleep(0.25)
        raise SentinelError("login succeeded but the session never became usable")

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One request; signs in again once if the session expired (long runs)."""
        headers = {"X-CSRF-Token": self._csrf, **kwargs.pop("headers", {})}
        for attempt in (1, 2, 3):
            try:
                r = self._http.request(method, path, headers=headers, **kwargs)
            except httpx.TransportError:
                if attempt == 3:
                    raise
                time.sleep(2 * attempt)
                continue
            if r.status_code == 401 and attempt == 1:
                self.login()
                headers["X-CSRF-Token"] = self._csrf
                continue
            return r
        raise AssertionError("unreachable")  # pragma: no cover

    def json(self, method: str, path: str, *, ok: tuple[int, ...] = (200,), **kw: Any) -> Any:
        r = self.request(method, path, **kw)
        if r.status_code not in ok:
            raise SentinelError(f"{method} {path}: HTTP {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    # ------------------------------------------------------------- reading
    def incidents_since(self, since: datetime) -> list[dict[str, Any]]:
        """Every incident detected at or after ``since`` (newest first, paged)."""
        found: list[dict[str, Any]] = []
        for page in self._incident_pages():
            for incident in page:
                if datetime.fromisoformat(incident["detected_at"]) < since:
                    return found
                found.append(incident)
        return found

    def _incident_pages(self) -> Iterator[list[dict[str, Any]]]:
        cursor: int | None = None
        while True:
            params: dict[str, Any] = {"limit": 200}
            if cursor is not None:
                params["cursor"] = cursor
            page: list[dict[str, Any]] = self.json("GET", "/api/v1/incidents", params=params)
            if not page:
                return
            yield page
            cursor = min(i["number"] for i in page)

    def incident(self, incident_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self.json("GET", f"/api/v1/incidents/{incident_id}")
        return result

    def signals_since(self, since: datetime, **params: Any) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self.json(
            "GET", "/api/v1/signals", params={"since": since.isoformat(), "limit": 500, **params}
        )
        return result

    def integration_status(self, integration_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self.json("GET", f"/api/v1/integrations/{integration_id}/status")
        return result

    # ------------------------------------------------------------- writing
    def set_incident_status(self, incident_id: str, status: str, note: str) -> bool:
        r = self.request(
            "PATCH", f"/api/v1/incidents/{incident_id}", json={"status": status, "note": note}
        )
        return r.status_code == 200

    def create_integration(self, **body: Any) -> tuple[str, str]:
        data = self.json("POST", "/api/v1/integrations", ok=(201,), json=body)
        return str(data["integration"]["id"]), str(data["ingest_token"])

    def disable_integration(self, integration_id: str) -> None:
        self.request("DELETE", f"/api/v1/integrations/{integration_id}")

    def create_check(self, **body: Any) -> str:
        return str(self.json("POST", "/api/v1/checks", ok=(201,), json=body)["id"])

    def create_api_key(self, name: str, scopes: list[str]) -> str:
        data = self.json(
            "POST", "/api/v1/api-keys", ok=(201,), json={"name": name, "scopes": scopes}
        )
        return str(data["secret"])

    def post_deployment(self, key: str, event: dict[str, Any], idempotency_key: str) -> int:
        r = self._http.post(
            "/api/v1/ingest/deployments",
            json=event,
            headers={"X-Sentinel-Key": key, "Idempotency-Key": idempotency_key},
        )
        return r.status_code


def short_id() -> str:
    return uuid.uuid4().hex[:8]
