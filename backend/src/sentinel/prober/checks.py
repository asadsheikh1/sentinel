"""HTTP(S), TCP and TLS checks. Every connection goes to an IP vetted by the guard.

Rules that keep a probe from being turned into something else:

* GET only, no request body, no cookies, no credentials, no proxy from the
  environment, a fixed User-Agent.
* Redirects are followed by hand (at most 5), and each hop is vetted again.
* At most 64 KiB of the body is read, and only to evaluate assertions; it is
  never stored or sent anywhere.
* The whole check (DNS included) is bounded by the check's timeout.
"""

import asyncio
import contextlib
import json
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import SplitResult, urljoin, urlunsplit

import httpx

from sentinel.prober.ssrf_guard import (
    MAX_REDIRECTS,
    BlockedTargetError,
    Guard,
    UnresolvableTargetError,
    Vetted,
    split_host_port,
)

CheckKind = Literal["http", "tcp", "tls"]
USER_AGENT = "SENTINEL-Prober/0.1 (+health check)"
MAX_BODY_BYTES = 64 * 1024


@dataclass(frozen=True)
class CheckSpec:
    id: str
    name: str
    kind: CheckKind
    target: str
    interval_s: int
    timeout_s: float
    integration_id: str
    assertions: dict[str, Any] = field(default_factory=dict)


@dataclass
class Result:
    check: CheckSpec
    success: bool
    duration_s: float
    started_ns: int
    error: str | None = None
    status_code: int | None = None
    cert_expiry_s: float | None = None  # notAfter as a Unix timestamp
    ip: str | None = None
    redirects: int = 0


def _expiry(ssl_object: ssl.SSLObject | ssl.SSLSocket | None) -> float | None:
    if ssl_object is None:
        return None
    cert = ssl_object.getpeercert()
    not_after = cert.get("notAfter") if cert else None
    return float(ssl.cert_time_to_seconds(str(not_after))) if not_after else None


def _describe(exc: BaseException) -> str:
    """A short, user-safe reason. No body, no headers, no stack."""
    if isinstance(exc, UnresolvableTargetError):
        return f"dns: {exc}"
    if isinstance(exc, BlockedTargetError):
        return f"blocked: {exc}"
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return f"tls: {exc.verify_message or 'certificate verify failed'}"
    if isinstance(exc, ssl.SSLError):
        return f"tls: {exc.reason or 'handshake failed'}"
    if isinstance(exc, httpx.ConnectError):
        cause = exc.__cause__ or exc.__context__
        while cause is not None and not isinstance(cause, ssl.SSLError | OSError):
            cause = cause.__cause__ or cause.__context__
        if isinstance(cause, ssl.SSLError):
            return _describe(cause)
        return "connect: connection failed"
    if isinstance(exc, ConnectionRefusedError):
        return "connect: connection refused"
    if isinstance(exc, OSError):
        return f"connect: {exc.strerror or type(exc).__name__}"
    if isinstance(exc, httpx.HTTPError):
        return f"http: {type(exc).__name__}"
    return f"error: {type(exc).__name__}"


def _status_ok(status: int, expected: list[int]) -> bool:
    return status in expected if expected else 200 <= status < 400


def _json_field(body: bytes, path: str) -> Any:
    value: Any = json.loads(body)
    for part in path.split("."):
        if isinstance(value, list) and part.isdigit():
            value = value[int(part)]
        elif isinstance(value, dict):
            value = value[part]
        else:
            raise KeyError(part)
    return value


def evaluate_http(status: int, body: bytes, assertions: dict[str, Any]) -> str | None:
    """None if all assertions hold, else the first failure."""
    expected = list(assertions.get("expected_status") or [])
    if not _status_ok(status, expected):
        wanted = ", ".join(map(str, expected)) if expected else "200-399"
        return f"status {status}, expected {wanted}"
    if (contains := assertions.get("body_contains")) and str(contains).encode() not in body:
        return "body does not contain the expected text"
    if spec := assertions.get("json_field"):
        try:
            actual = _json_field(body, str(spec["path"]))
        except (ValueError, KeyError, IndexError, TypeError):
            return f"JSON field {spec['path']!r} not found"
        if "equals" in spec and actual != spec["equals"]:
            return f"JSON field {spec['path']!r} has an unexpected value"
    return None


def _pinned_url(parts: SplitResult, vetted: Vetted) -> tuple[str, str]:
    """(URL with the vetted IP as host, Host header)."""
    ip = f"[{vetted.ip}]" if vetted.ip.version == 6 else str(vetted.ip)
    default = 443 if parts.scheme == "https" else 80
    netloc = ip if vetted.port == default else f"{ip}:{vetted.port}"
    host_header = vetted.host if vetted.port == default else f"{vetted.host}:{vetted.port}"
    if ":" in vetted.host and not vetted.host.startswith("["):  # IPv6 literal target
        host_header = f"[{vetted.host}]" + ("" if vetted.port == default else f":{vetted.port}")
    path = parts.path or "/"
    return urlunsplit((parts.scheme, netloc, path, parts.query, "")), host_header


async def run_http(
    spec: CheckSpec,
    guard: Guard,
    ssl_context: ssl.SSLContext,
    result: Result,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    url = spec.target
    async with httpx.AsyncClient(
        transport=transport,  # tests only; production uses httpx's own
        trust_env=False,  # never a proxy from the environment
        verify=ssl_context,
        follow_redirects=False,
        timeout=httpx.Timeout(spec.timeout_s),
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    ) as client:
        while True:
            parts, vetted = await guard.vet_url(url)
            result.ip = str(vetted.ip)
            pinned, host_header = _pinned_url(parts, vetted)
            extensions = {"sni_hostname": vetted.host} if parts.scheme == "https" else {}
            request = client.build_request(
                "GET", pinned, headers={"Host": host_header}, extensions=extensions
            )
            response = await client.send(request, stream=True)
            try:
                if parts.scheme == "https" and result.cert_expiry_s is None:
                    stream = response.extensions.get("network_stream")
                    if stream is not None:
                        result.cert_expiry_s = _expiry(stream.get_extra_info("ssl_object"))
                if response.is_redirect and "location" in response.headers:
                    result.redirects += 1
                    if result.redirects > MAX_REDIRECTS:
                        result.status_code = response.status_code
                        result.error = f"more than {MAX_REDIRECTS} redirects"
                        return
                    url = urljoin(url, response.headers["location"])
                    continue
                body = b""
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) >= MAX_BODY_BYTES:
                        body = body[:MAX_BODY_BYTES]
                        break
            finally:
                await response.aclose()
            result.status_code = response.status_code
            result.error = evaluate_http(response.status_code, body, spec.assertions)
            return


async def run_tcp(spec: CheckSpec, guard: Guard, result: Result) -> None:
    host, port = split_host_port(spec.target)
    vetted = await guard.vet(host, port)
    result.ip = str(vetted.ip)
    _, writer = await asyncio.open_connection(str(vetted.ip), port)
    writer.close()
    await writer.wait_closed()


async def run_tls(
    spec: CheckSpec, guard: Guard, ssl_context: ssl.SSLContext, result: Result
) -> None:
    host, port = split_host_port(spec.target, default_port=443)
    vetted = await guard.vet(host, port)
    result.ip = str(vetted.ip)
    _, writer = await asyncio.open_connection(
        str(vetted.ip), port, ssl=ssl_context, server_hostname=vetted.host
    )
    try:
        result.cert_expiry_s = _expiry(writer.get_extra_info("ssl_object"))
    finally:
        writer.close()
        with contextlib.suppress(ssl.SSLError, OSError):
            await writer.wait_closed()
    min_days = spec.assertions.get("min_days_valid")
    if min_days and result.cert_expiry_s is not None:
        days = (result.cert_expiry_s - time.time()) / 86400
        if days < float(min_days):
            result.error = f"certificate expires in {days:.0f} days (minimum {min_days})"


def default_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


async def run_check(
    spec: CheckSpec,
    guard: Guard,
    ssl_context: ssl.SSLContext | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Result:
    """Run one check. Never raises: every failure becomes ``success=False``."""
    ctx = ssl_context or default_ssl_context()
    result = Result(check=spec, success=False, duration_s=0.0, started_ns=time.time_ns())
    started = time.perf_counter()
    try:
        async with asyncio.timeout(spec.timeout_s):
            if spec.kind == "http":
                await run_http(spec, guard, ctx, result, transport)
            elif spec.kind == "tcp":
                await run_tcp(spec, guard, result)
            else:
                await run_tls(spec, guard, ctx, result)
        result.success = result.error is None
    except Exception as exc:
        result.error = _describe(exc)
    result.duration_s = time.perf_counter() - started
    return result
