"""Check behaviour against real local servers: ok, slow, 500, bad and expiring certs.

The servers listen on 127.0.0.1, which the guard blocks; the tests allow exactly
the test host name (``probe.test``), the same mechanism the dev demo uses.
"""

import asyncio
import ssl
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import trustme

from sentinel.prober.checks import CheckSpec, evaluate_http, run_check
from sentinel.prober.ssrf_guard import Guard

HOST = "probe.test"


async def _resolve(host: str, port: int) -> list[str]:
    return ["127.0.0.1"] if host == HOST else []


GUARD = Guard(resolver=_resolve, allow_hosts=frozenset({HOST}))
SEEN_HOST_HEADERS: list[str] = []


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
        writer.close()
        return
    lines = head.decode().split("\r\n")
    method, path, _ = lines[0].split(" ", 2)
    headers = {k.lower(): v for k, _, v in (line.partition(": ") for line in lines[1:] if line)}
    SEEN_HOST_HEADERS.append(headers.get("host", ""))
    status, extra, body = 200, "", b"hello sentinel"
    if path == "/500":
        status, body = 500, b"boom"
    elif path == "/slow":
        await asyncio.sleep(3)
    elif path == "/json":
        body = b'{"status": "ok", "checks": [{"db": true}]}'
    elif path == "/redirect":
        status, extra, body = 302, "Location: /json\r\n", b""
    elif path == "/big":
        body = b"x" * (1024 * 1024)
    elif path == "/method":
        body = method.encode()
    reason = {200: "OK", 302: "Found", 500: "Internal Server Error"}[status]
    writer.write(
        f"HTTP/1.1 {status} {reason}\r\nContent-Length: {len(body)}\r\n{extra}"
        "Connection: close\r\n\r\n".encode()
        + body
    )
    try:
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    except (ConnectionError, ssl.SSLError):
        pass


async def _serve(ssl_ctx: ssl.SSLContext | None = None) -> tuple[asyncio.Server, int]:
    server = await asyncio.start_server(_handle, "127.0.0.1", 0, ssl=ssl_ctx)
    return server, server.sockets[0].getsockname()[1]


@pytest.fixture
async def http_port() -> AsyncIterator[int]:
    server, port = await _serve()
    yield port
    server.close()


@pytest.fixture(scope="module")
def ca() -> trustme.CA:
    return trustme.CA()


def _server_ctx(ca: trustme.CA, **kwargs: datetime) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert(HOST, **kwargs).configure_cert(ctx)
    return ctx


def _client_ctx(ca: trustme.CA) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ca.configure_trust(ctx)
    return ctx


def spec(target: str, kind: str = "http", timeout: float = 2, **assertions: object) -> CheckSpec:
    return CheckSpec("c1", "test", kind, target, 30, timeout, "i1", dict(assertions))  # type: ignore[arg-type]


# ------------------------------------------------------------------ HTTP


async def test_ok(http_port: int) -> None:
    SEEN_HOST_HEADERS.clear()
    r = await run_check(spec(f"http://{HOST}:{http_port}/"), GUARD)
    assert r.success, r.error
    assert r.status_code == 200
    assert 0 < r.duration_s < 2
    assert r.ip == "127.0.0.1"
    assert [f"{HOST}:{http_port}"] == SEEN_HOST_HEADERS  # name, not the pinned IP


async def test_only_get_is_used(http_port: int) -> None:
    r = await run_check(spec(f"http://{HOST}:{http_port}/method", body_contains="GET"), GUARD)
    assert r.success, r.error


async def test_500_fails(http_port: int) -> None:
    r = await run_check(spec(f"http://{HOST}:{http_port}/500"), GUARD)
    assert not r.success
    assert r.status_code == 500
    assert r.error == "status 500, expected 200-399"


async def test_expected_status_can_be_set(http_port: int) -> None:
    r = await run_check(spec(f"http://{HOST}:{http_port}/500", expected_status=[500]), GUARD)
    assert r.success, r.error


async def test_slow_server_times_out_within_the_check_timeout(http_port: int) -> None:
    started = time.perf_counter()
    r = await run_check(spec(f"http://{HOST}:{http_port}/slow", timeout=1), GUARD)
    assert not r.success
    assert r.error == "timeout"
    assert time.perf_counter() - started < 1.5


async def test_connection_refused(http_port: int) -> None:
    server, port = await _serve()
    server.close()
    await server.wait_closed()
    r = await run_check(spec(f"http://{HOST}:{port}/"), GUARD)
    assert not r.success
    assert (r.error or "").startswith("connect:"), r.error


async def test_redirects_are_followed_and_counted(http_port: int) -> None:
    r = await run_check(
        spec(
            f"http://{HOST}:{http_port}/redirect",
            json_field={"path": "checks.0.db", "equals": True},
        ),
        GUARD,
    )
    assert r.success, r.error
    assert r.redirects == 1


async def test_body_and_json_assertions(http_port: int) -> None:
    base = f"http://{HOST}:{http_port}"
    assert (await run_check(spec(f"{base}/", body_contains="sentinel"), GUARD)).success
    r = await run_check(spec(f"{base}/", body_contains="nope"), GUARD)
    assert r.error == "body does not contain the expected text"
    r = await run_check(
        spec(f"{base}/json", json_field={"path": "status", "equals": "down"}), GUARD
    )
    assert r.error == "JSON field 'status' has an unexpected value"
    r = await run_check(spec(f"{base}/json", json_field={"path": "missing"}), GUARD)
    assert r.error == "JSON field 'missing' not found"


async def test_large_bodies_are_capped(http_port: int) -> None:
    r = await run_check(spec(f"http://{HOST}:{http_port}/big", body_contains="x"), GUARD)
    assert r.success, r.error


def test_evaluate_http_rules() -> None:
    assert evaluate_http(204, b"", {}) is None
    assert evaluate_http(404, b"", {}) == "status 404, expected 200-399"
    assert evaluate_http(404, b"", {"expected_status": [404]}) is None
    assert evaluate_http(200, b"not json", {"json_field": {"path": "a"}}) == (
        "JSON field 'a' not found"
    )


# ------------------------------------------------------------------ TLS


async def test_https_with_a_trusted_cert_reports_expiry(ca: trustme.CA) -> None:
    server, port = await _serve(_server_ctx(ca))
    try:
        r = await run_check(spec(f"https://{HOST}:{port}/"), GUARD, _client_ctx(ca))
    finally:
        server.close()
    assert r.success, r.error
    assert (r.cert_expiry_s or 0) > time.time()


async def test_bad_cert_fails(ca: trustme.CA) -> None:
    """An untrusted issuer: the default trust store doesn't know this CA."""
    server, port = await _serve(_server_ctx(ca))
    try:
        r = await run_check(spec(f"https://{HOST}:{port}/"), GUARD)
        t = await run_check(spec(f"{HOST}:{port}", kind="tls"), GUARD)
    finally:
        server.close()
    assert not r.success
    assert (r.error or "").startswith("tls:"), r.error
    assert not t.success
    assert (t.error or "").startswith("tls:"), t.error


async def test_wrong_host_name_fails(ca: trustme.CA) -> None:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert("other.test").configure_cert(ctx)
    server, port = await _serve(ctx)
    try:
        r = await run_check(spec(f"https://{HOST}:{port}/"), GUARD, _client_ctx(ca))
    finally:
        server.close()
    assert not r.success
    assert "tls:" in (r.error or "")


async def test_expired_cert_fails(ca: trustme.CA) -> None:
    past = datetime.now(UTC) - timedelta(days=2)
    server, port = await _serve(_server_ctx(ca, not_after=past))
    try:
        r = await run_check(spec(f"{HOST}:{port}", kind="tls"), GUARD, _client_ctx(ca))
    finally:
        server.close()
    assert not r.success
    assert "expired" in (r.error or ""), r.error


async def test_tls_check_reports_expiry_and_minimum_days(ca: trustme.CA) -> None:
    soon = datetime.now(UTC) + timedelta(days=5)
    server, port = await _serve(_server_ctx(ca, not_after=soon))
    try:
        ok = await run_check(spec(f"{HOST}:{port}", kind="tls"), GUARD, _client_ctx(ca))
        short = await run_check(
            spec(f"{HOST}:{port}", kind="tls", min_days_valid=14), GUARD, _client_ctx(ca)
        )
    finally:
        server.close()
    assert ok.success, ok.error
    assert ok.cert_expiry_s is not None
    assert abs(ok.cert_expiry_s - soon.timestamp()) < 120
    assert not short.success
    assert (short.error or "").startswith("certificate expires in 5 days")


# ------------------------------------------------------------------ TCP


async def test_tcp_open_and_closed_ports(http_port: int) -> None:
    assert (await run_check(spec(f"{HOST}:{http_port}", kind="tcp"), GUARD)).success
    server, port = await _serve()
    server.close()
    await server.wait_closed()
    r = await run_check(spec(f"{HOST}:{port}", kind="tcp"), GUARD)
    assert not r.success
    assert (r.error or "").startswith("connect:")
