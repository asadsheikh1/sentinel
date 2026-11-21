"""SSRF guard for user-configured probe targets (ADR-0015, threat T12).

A probe target is checked twice: when a check is created (here, in the API) and
before every connection (in the prober). Only public unicast addresses pass:

1. **Host syntax.** URL credentials, single-label names (`postgres`, `api`),
   `localhost` and internal suffixes (`.internal`, `.local`, `.localhost`, …)
   and ambiguous numeric forms (`2130706433`, `0177.0.0.1`, `0x7f.1`) are refused
   before any DNS lookup.
2. **Resolve, then validate every address.** All A/AAAA answers must be public.
   A single private answer rejects the target (no "pick the good one"). IPv6
   forms that embed an IPv4 address (mapped, NAT64, 6to4, Teredo) are judged by
   the embedded address too.
3. **Pin.** The caller connects to the validated IP, never to the name again, so
   a DNS answer that changes between check and connect (rebinding) can't redirect
   the connection. TLS still verifies the certificate against the host name.
4. **Redirects** are followed by the caller one hop at a time, each hop going
   through 1-3 again.

``allow_hosts`` is a development escape hatch (exact host names, e.g. the demo
shop on the local machine). The settings refuse it in production.
"""

import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import SplitResult, urlsplit

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]

# Not covered by `is_global` (or covered only in some Python versions): stated
# explicitly so the policy doesn't move with the interpreter.
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",  # "this network"
        "10.0.0.0/8",
        "100.64.0.0/10",  # CGNAT (and the Alibaba metadata address 100.100.100.200)
        "127.0.0.0/8",
        "169.254.0.0/16",  # link-local, AWS/GCP/Azure metadata 169.254.169.254
        "172.16.0.0/12",
        "192.0.0.0/24",  # IETF protocol assignments (incl. 192.0.0.192 Oracle metadata)
        "192.0.2.0/24",
        "192.88.99.0/24",  # 6to4 relay anycast
        "192.168.0.0/16",
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved, incl. broadcast
        "::/128",
        "::1/128",
        "::ffff:0:0/96",  # IPv4-mapped: judged by the embedded address as well
        "64:ff9b:1::/48",  # local-use NAT64
        "100::/64",  # discard
        "2001::/23",  # IETF protocol assignments (Teredo is judged by its embedded address)
        "2001:db8::/32",
        "fc00::/7",  # unique local, incl. AWS IMDS over IPv6 fd00:ec2::254
        "fe80::/10",  # link-local
        "fec0::/10",  # site-local (deprecated)
        "ff00::/8",  # multicast
    )
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")

_BLOCKED_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})
_BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".intranet",
    ".lan",
    ".home",
    ".corp",
    ".private",
    ".localdomain",
    ".home.arpa",
    ".in-addr.arpa",
    ".ip6.arpa",
)
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
# Anything made of digits, dots and hex markers that isn't a canonical dotted quad
# (inet_aton accepts 2130706433, 0177.0.0.1, 0x7f.1, 127.1 as 127.0.0.1).
_NUMERIC_ISH = re.compile(r"^(0x[0-9a-f]*|[0-9]+)(\.(0x[0-9a-f]*|[0-9]*))*\.?$")

MAX_REDIRECTS = 5
ALLOWED_SCHEMES = frozenset({"http", "https"})


class BlockedTargetError(Exception):
    """The target is not allowed. The message is safe to show to the user."""


class UnresolvableTargetError(BlockedTargetError):
    """The name has no address (an outage, not a policy decision)."""


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    found: list[ipaddress.IPv4Address] = []
    if ip.ipv4_mapped is not None:
        found.append(ip.ipv4_mapped)
    if ip.sixtofour is not None:
        found.append(ip.sixtofour)
    if ip.teredo is not None:
        found.extend(ip.teredo)
    if ip in _NAT64:
        found.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return found


def is_blocked_ip(ip: IPAddress) -> bool:
    """True unless ``ip`` is a public unicast address (embedded IPv4 included)."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.scope_id:
            return True
        if any(is_blocked_ip(v4) for v4 in _embedded_ipv4(ip)):
            return True
    if any(ip in net for net in _BLOCKED_NETWORKS if net.version == ip.version):
        return True
    return not ip.is_global or ip.is_multicast


def check_ip(value: str) -> IPAddress:
    try:
        ip = ipaddress.ip_address(value)  # "fe80::1%eth0" keeps its scope (and is blocked)
    except ValueError as e:
        raise BlockedTargetError(f"Not an IP address: {value}") from e
    if is_blocked_ip(ip):
        raise BlockedTargetError(f"{ip} is a private, local or reserved address.")
    return ip


def _literal_ip(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def check_host(host: str, allow_hosts: Iterable[str] = ()) -> IPAddress | None:
    """Validate a host name or IP literal without DNS. Returns the IP if literal."""
    host = host.strip().lower().rstrip(".")
    if not host:
        raise BlockedTargetError("The target has no host.")
    if host in {h.lower() for h in allow_hosts}:
        return _literal_ip(host)
    ip = _literal_ip(host)
    if ip is not None:
        if is_blocked_ip(ip):
            raise BlockedTargetError(f"{ip} is a private, local or reserved address.")
        if isinstance(ip, ipaddress.IPv4Address) and str(ip) != host:
            raise BlockedTargetError("Use the canonical form of the IP address.")
        return ip
    if _NUMERIC_ISH.match(host):
        raise BlockedTargetError(f"{host!r} is an ambiguous numeric address.")
    if host in _BLOCKED_NAMES or host.endswith(_BLOCKED_SUFFIXES):
        raise BlockedTargetError(f"{host!r} is a local or internal name.")
    if len(host) > 253:
        raise BlockedTargetError("The host name is too long.")
    labels = host.split(".")
    if len(labels) < 2:
        raise BlockedTargetError(f"{host!r} is not a fully qualified public name.")
    for label in labels:
        try:
            ascii_label = label.encode("idna").decode("ascii")
        except UnicodeError as e:
            raise BlockedTargetError(f"{host!r} is not a valid host name.") from e
        if not _LABEL.match(ascii_label):
            raise BlockedTargetError(f"{host!r} is not a valid host name.")
    if labels[-1].isdigit():
        raise BlockedTargetError(f"{host!r} is not a valid host name.")
    return None


async def system_resolver(host: str, port: int) -> list[str]:
    import asyncio

    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    return [str(info[4][0]) for info in infos]


@dataclass(frozen=True)
class Vetted:
    """A target that passed the guard: connect to ``ip``, present ``host``."""

    host: str
    ip: IPAddress
    port: int


@dataclass
class Guard:
    resolver: Resolver = system_resolver
    allow_hosts: frozenset[str] = field(default_factory=frozenset)

    async def vet(self, host: str, port: int) -> Vetted:
        if not 1 <= port <= 65535:
            raise BlockedTargetError("The port must be 1-65535.")
        host = host.strip().lower().rstrip(".").strip("[]")
        allowed = host in self.allow_hosts
        literal = check_host(host, self.allow_hosts)
        if literal is not None:
            return Vetted(host, literal, port)
        try:
            answers = await self.resolver(host, port)
        except OSError as e:
            raise UnresolvableTargetError(f"{host!r} does not resolve.") from e
        if not answers:
            raise UnresolvableTargetError(f"{host!r} does not resolve.")
        ips: list[IPAddress] = []
        for answer in dict.fromkeys(answers):  # de-duplicate, keep order
            try:
                ip = ipaddress.ip_address(answer)
            except ValueError as e:
                raise BlockedTargetError(f"{host!r} resolved to an invalid address.") from e
            if not allowed and is_blocked_ip(ip):
                # One bad answer rejects the name: don't let a resolver mix in an
                # internal address and hope we pick it later.
                raise BlockedTargetError(
                    f"{host!r} resolves to {ip}, a private, local or reserved address."
                )
            ips.append(ip)
        # Prefer IPv4 (most probe targets), keep the resolver's order otherwise.
        ips.sort(key=lambda a: a.version)
        return Vetted(host, ips[0], port)

    async def vet_url(self, url: str) -> tuple[SplitResult, Vetted]:
        parts = parse_url(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return parts, await self.vet(parts.hostname or "", port)


def parse_url(url: str) -> SplitResult:
    """Parse and check the parts of a probe URL that don't need DNS."""
    if len(url) > 2048:
        raise BlockedTargetError("The URL is too long.")
    if any(c in url for c in "\r\n\t\\") or any(ord(c) < 0x21 for c in url):
        raise BlockedTargetError("The URL contains invalid characters.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise BlockedTargetError(f"Invalid URL: {e}") from e
    if parts.scheme not in ALLOWED_SCHEMES:
        raise BlockedTargetError("Only http:// and https:// URLs can be checked.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise BlockedTargetError("URLs with credentials are not allowed.")
    if not parts.hostname:
        raise BlockedTargetError("The URL has no host.")
    if port is not None and not 1 <= port <= 65535:
        raise BlockedTargetError("The port must be 1-65535.")
    return parts


def split_host_port(target: str, default_port: int | None = None) -> tuple[str, int]:
    """``host:port`` or ``[v6]:port`` for TCP and TLS checks."""
    target = target.strip()
    if target.startswith("["):
        host, _, rest = target[1:].partition("]")
        port_text = rest.removeprefix(":")
    else:
        host, sep, port_text = target.rpartition(":")
        if not sep:
            host, port_text = target, ""
        elif ":" in host:
            raise BlockedTargetError("Write IPv6 addresses in brackets: [2001:db8::1]:443.")
    if not port_text:
        if default_port is None:
            raise BlockedTargetError("The target needs a port (host:port).")
        return host, default_port
    if not port_text.isdigit():
        raise BlockedTargetError("The port must be a number.")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise BlockedTargetError("The port must be 1-65535.")
    return host, port
