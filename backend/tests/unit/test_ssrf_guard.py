"""SSRF suite (roadmap Step 9, threat T12): every listed case must be blocked.

Uses a fake resolver, so nothing here touches the network.
"""

import ipaddress
from collections.abc import Awaitable, Callable

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from sentinel.prober.checks import CheckSpec, run_check
from sentinel.prober.ssrf_guard import (
    BlockedTargetError,
    Guard,
    UnresolvableTargetError,
    check_host,
    is_blocked_ip,
    parse_url,
    split_host_port,
)

PUBLIC = "93.184.215.14"
PUBLIC6 = "2606:2800:21f:cb07:6820:80da:af6b:8b2c"


def resolver(table: dict[str, list[str]]) -> Callable[[str, int], Awaitable[list[str]]]:
    async def resolve(host: str, port: int) -> list[str]:
        if host not in table:
            raise OSError("NXDOMAIN")
        return table[host]

    return resolve


# ------------------------------------------------------------------ addresses

BLOCKED_IPS = [
    # IPv4 loopback, private, link-local, CGNAT, metadata, reserved
    "127.0.0.1", "127.255.255.254", "0.0.0.0", "10.0.0.1", "10.255.255.255",  # noqa: S104
    "172.16.0.1", "172.31.255.255", "192.168.1.1", "169.254.169.254", "169.254.0.1",
    "100.64.0.1", "100.100.100.200", "192.0.0.192", "192.0.2.1", "198.18.0.1",
    "198.51.100.1", "203.0.113.1", "224.0.0.1", "239.255.255.250", "240.0.0.1",
    "255.255.255.255",
    # IPv6 loopback, unspecified, ULA (incl. AWS IMDS), link-local, multicast, docs
    "::1", "::", "fd00:ec2::254", "fc00::1", "fe80::1", "ff02::1", "2001:db8::1",
    "fec0::1", "100::1",
    # IPv6 forms that embed a private IPv4 address
    "::ffff:127.0.0.1", "::ffff:169.254.169.254", "::ffff:10.0.0.1",
    "64:ff9b::7f00:1",  # NAT64 -> 127.0.0.1 (Python calls this "global")
    "64:ff9b::a9fe:a9fe",  # NAT64 -> 169.254.169.254
    "64:ff9b:1::1",
    "2002:7f00:1::1",  # 6to4 -> 127.0.0.1
    "2002:a9fe:a9fe::1",  # 6to4 -> 169.254.169.254
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
]  # fmt: skip

ALLOWED_IPS = [PUBLIC, "8.8.8.8", "1.1.1.1", PUBLIC6, "2606:4700:4700::1111"]


@pytest.mark.parametrize("value", BLOCKED_IPS)
def test_private_local_and_reserved_addresses_are_blocked(value: str) -> None:
    assert is_blocked_ip(ipaddress.ip_address(value))


@pytest.mark.parametrize("value", ALLOWED_IPS)
def test_public_addresses_are_allowed(value: str) -> None:
    assert not is_blocked_ip(ipaddress.ip_address(value))


def test_scoped_ipv6_is_blocked() -> None:
    assert is_blocked_ip(ipaddress.ip_address("fe80::1%eth0"))


# ------------------------------------------------------------------ host syntax

BLOCKED_HOSTS = [
    "localhost", "LOCALHOST", "localhost.", "foo.localhost", "ip6-localhost",
    "metadata.google.internal", "db.internal", "printer.local", "nas.lan",
    "router.home.arpa", "intranet.corp",
    "postgres", "api", "caddy", "gateway",  # single-label: container and service names
    # ambiguous numeric forms of 127.0.0.1 / 169.254.169.254
    "2130706433", "0x7f000001", "0177.0.0.1", "0x7f.0.0.1", "127.1", "127.0.1",
    "017700000001", "2852039166", "0xa9fea9fe", "0251.0376.0251.0376",
    "1.2.3", "1.2.3.4.5",
    # literal private IPs, bracketed IPv6
    "127.0.0.1", "169.254.169.254", "[::1]", "::1", "[fd00:ec2::254]", "[::ffff:127.0.0.1]",
    # malformed
    "-bad.example.com", "bad-.example.com", "a..b.com", "", "exa mple.com",
]  # fmt: skip


@pytest.mark.parametrize("host", BLOCKED_HOSTS)
def test_unsafe_host_names_are_refused_before_dns(host: str) -> None:
    with pytest.raises(BlockedTargetError):
        check_host(host)


@pytest.mark.parametrize("host", ["example.com", "api.example.com", "xn--bcher-kva.example"])
def test_public_names_pass_syntax(host: str) -> None:
    assert check_host(host) is None


# ------------------------------------------------------------------ URLs

BLOCKED_URLS = [
    "http://127.0.0.1/", "http://localhost:8000/", "http://[::1]/", "http://169.254.169.254/latest/meta-data/",
    "http://2130706433/", "http://0177.0.0.1/", "http://0x7f.1/", "http://10.0.0.5:5432/",
    "http://[::ffff:127.0.0.1]/", "http://[fd00:ec2::254]/latest/meta-data/",
    "http://user:pass@example.com/", "http://example.com@127.0.0.1/",
    "file:///etc/passwd", "gopher://example.com/", "ftp://example.com/", "dict://example.com:11211/",
    "javascript:alert(1)", "http:///nohost", "http://example.com:0/", "http://example.com:99999/",
    "http://exa\nmple.com/", "http://example.com/\r\nX-Injected: 1",
]  # fmt: skip


@pytest.mark.parametrize("url", BLOCKED_URLS)
async def test_unsafe_urls_are_refused(url: str) -> None:
    guard = Guard(resolver=resolver({"example.com": [PUBLIC]}))
    with pytest.raises(BlockedTargetError):
        await guard.vet_url(url)


async def test_a_public_url_is_vetted_and_pinned() -> None:
    guard = Guard(resolver=resolver({"shop.example.com": [PUBLIC, PUBLIC6]}))
    parts, vetted = await guard.vet_url("https://shop.example.com/health?x=1")
    assert (vetted.host, str(vetted.ip), vetted.port) == ("shop.example.com", PUBLIC, 443)
    assert parts.path == "/health"


# ------------------------------------------------------------------ DNS


@pytest.mark.parametrize(
    "answers",
    [
        ["127.0.0.1"],
        ["169.254.169.254"],
        ["10.0.0.7"],
        ["::1"],
        ["fd00:ec2::254"],
        [PUBLIC, "127.0.0.1"],  # one bad answer is enough
        ["::ffff:192.168.0.1", PUBLIC],
        ["64:ff9b::a9fe:a9fe"],
        ["not-an-ip"],
        [],
    ],
)
async def test_names_resolving_to_unsafe_addresses_are_refused(answers: list[str]) -> None:
    guard = Guard(resolver=resolver({"evil.example.com": answers}))
    with pytest.raises(BlockedTargetError):
        await guard.vet("evil.example.com", 80)


async def test_unresolvable_names_are_refused_and_reported_as_dns() -> None:
    with pytest.raises(UnresolvableTargetError, match="does not resolve"):
        await Guard(resolver=resolver({})).vet("nope.example.com", 80)
    spec = CheckSpec("c", "t", "http", "http://nope.example.com/", 30, 2, "i")
    result = await run_check(spec, Guard(resolver=resolver({})))
    assert result.error == "dns: 'nope.example.com' does not resolve."


async def test_dns_rebinding_cannot_move_the_connection() -> None:
    """The name is resolved once per hop; the request goes to the vetted IP only."""
    calls: list[str] = []
    answers = iter([[PUBLIC], ["127.0.0.1"], ["127.0.0.1"]])

    async def rebinding(host: str, port: int) -> list[str]:
        calls.append(host)
        return next(answers)

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    spec = CheckSpec("c1", "rebind", "http", "http://rebind.example.com/health", 30, 5, "i1")
    result = await run_check(
        spec, Guard(resolver=rebinding), transport=httpx.MockTransport(handler)
    )
    assert result.success, result.error
    assert calls == ["rebind.example.com"]
    assert len(seen) == 1
    assert seen[0].url.host == PUBLIC  # pinned: the name is never looked up again
    assert seen[0].headers["host"] == "rebind.example.com"
    assert result.ip == PUBLIC


async def test_redirect_to_a_private_address_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})

    spec = CheckSpec("c1", "redir", "http", "http://shop.example.com/", 30, 5, "i1")
    result = await run_check(
        spec,
        Guard(resolver=resolver({"shop.example.com": [PUBLIC]})),
        transport=httpx.MockTransport(handler),
    )
    assert not result.success
    assert (result.error or "").startswith("blocked:")
    assert result.redirects == 1


@pytest.mark.parametrize(
    "location",
    [
        "http://localhost/",
        "http://127.0.0.1:8000/admin",
        "http://[::1]/",
        "http://2130706433/",
        "file:///etc/passwd",
        "http://internal.example.com/",  # resolves to a private address
    ],
)
async def test_every_redirect_hop_is_vetted_again(location: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "shop.example.com":
            return httpx.Response(301, headers={"Location": location})
        return httpx.Response(200)  # pragma: no cover - must never be reached

    table = {"shop.example.com": [PUBLIC], "internal.example.com": ["10.1.2.3"]}
    spec = CheckSpec("c1", "redir", "http", "http://shop.example.com/", 30, 5, "i1")
    result = await run_check(
        spec, Guard(resolver=resolver(table)), transport=httpx.MockTransport(handler)
    )
    assert not result.success
    assert (result.error or "").startswith("blocked:"), result.error


async def test_redirect_loops_stop_after_five_hops() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "/again"})

    spec = CheckSpec("c1", "loop", "http", "http://shop.example.com/", 30, 5, "i1")
    result = await run_check(
        spec,
        Guard(resolver=resolver({"shop.example.com": [PUBLIC]})),
        transport=httpx.MockTransport(handler),
    )
    assert not result.success
    assert result.error == "more than 5 redirects"


async def test_tcp_and_tls_targets_are_vetted_too() -> None:
    guard = Guard(resolver=resolver({"db.example.com": ["10.0.0.5"]}))
    for kind, target in [
        ("tcp", "127.0.0.1:22"),
        ("tcp", "db.example.com:5432"),
        ("tls", "[::1]:443"),
        ("tls", "169.254.169.254:443"),
        ("tcp", "localhost:6379"),
    ]:
        spec = CheckSpec("c", "t", kind, target, 30, 2, "i")  # type: ignore[arg-type]
        result = await run_check(spec, guard)
        assert not result.success
        assert (result.error or "").startswith("blocked:"), (target, result)


def test_allow_hosts_is_an_exact_name_match_only() -> None:
    assert check_host("aviro_gateway_1", allow_hosts={"aviro_gateway_1"}) is None
    with pytest.raises(BlockedTargetError):
        check_host("aviro_gateway_2", allow_hosts={"aviro_gateway_1"})
    with pytest.raises(BlockedTargetError):
        check_host("x.aviro_gateway_1", allow_hosts={"aviro_gateway_1"})


async def test_an_allowed_host_may_resolve_privately_but_nothing_else_may() -> None:
    table = {"aviro_gateway_1": ["10.89.0.5"], "other.example.com": ["10.89.0.5"]}
    guard = Guard(resolver=resolver(table), allow_hosts=frozenset({"aviro_gateway_1"}))
    assert str((await guard.vet("aviro_gateway_1", 8000)).ip) == "10.89.0.5"
    with pytest.raises(BlockedTargetError):
        await guard.vet("other.example.com", 80)


def test_host_port_parsing() -> None:
    assert split_host_port("example.com:443") == ("example.com", 443)
    assert split_host_port("[2606:4700::1111]:853") == ("2606:4700::1111", 853)
    assert split_host_port("example.com", default_port=443) == ("example.com", 443)
    for bad in ["example.com", "example.com:http", "example.com:0", "2606:4700::1111:443"]:
        with pytest.raises(BlockedTargetError):
            split_host_port(bad)


def test_url_parsing_keeps_path_and_query() -> None:
    parts = parse_url("https://example.com:8443/a/b?c=d")
    assert (parts.hostname, parts.port, parts.path, parts.query) == (
        "example.com",
        8443,
        "/a/b",
        "c=d",
    )


# ------------------------------------------------------------------ property

_V4 = st.ip_addresses(v=4)
_V6 = st.ip_addresses(v=6)


@given(_V4)
def test_any_non_global_ipv4_is_blocked(ip: ipaddress.IPv4Address) -> None:
    if not ip.is_global or ip.is_multicast:
        assert is_blocked_ip(ip)


@given(_V4)
def test_mapped_and_nat64_forms_inherit_the_ipv4_verdict(ip: ipaddress.IPv4Address) -> None:
    mapped = ipaddress.IPv6Address(f"::ffff:{ip}")
    nat64 = ipaddress.IPv6Address(int(ipaddress.IPv6Address("64:ff9b::")) | int(ip))
    assert is_blocked_ip(mapped)  # ::ffff:0:0/96 is never a probe target
    if is_blocked_ip(ip):
        assert is_blocked_ip(nat64)


@given(_V6)
def test_any_non_global_ipv6_is_blocked(ip: ipaddress.IPv6Address) -> None:
    if not ip.is_global or ip.is_multicast:
        assert is_blocked_ip(ip)


@pytest.mark.parametrize(
    "host", ["2130706433", "0x7f000001", "0177.0.0.1", "0x7f.0.0.1", "127.1", "0251.0376.0251.0376"]
)
def test_numeric_forms_are_named_as_such(host: str) -> None:
    """Other rules would also refuse these; this one gives the user the real reason."""
    with pytest.raises(BlockedTargetError, match="ambiguous numeric address"):
        check_host(host)
