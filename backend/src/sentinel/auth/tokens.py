"""Opaque tokens: session tokens, CSRF tokens, invite tokens and API keys.

Only ``HMAC-SHA256(secret_key, token)`` is stored. A database leak alone does
not let anyone log in or forge a key, because the pepper lives in the app's
environment, not in the database.
"""

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

API_KEY_PREFIX = "sk_live_"
_API_KEY_RE = re.compile(r"^sk_live_([a-z0-9]{8})_([A-Za-z0-9_-]{43})$")
_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def new_token() -> str:
    """256 bits of randomness, URL-safe."""
    return secrets.token_urlsafe(32)


def hash_token(secret_key: str, token: str, *, purpose: str) -> str:
    """Keyed hash; ``purpose`` separates domains (a session token is not an invite token)."""
    msg = f"{purpose}:{token}".encode()
    return hmac.new(secret_key.encode(), msg, hashlib.sha256).hexdigest()


def csrf_token_for(secret_key: str, session_token_hash: str) -> str:
    """Signed double-submit CSRF token, bound to the session.

    The browser gets it in a readable cookie and must echo it in the
    ``X-CSRF-Token`` header. An attacker's page can't read our cookie, and a
    token from another session won't verify.
    """
    return hash_token(secret_key, session_token_hash, purpose="csrf")


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


@dataclass(frozen=True)
class NewApiKey:
    plaintext: str  # shown to the user exactly once
    prefix: str  # stored, for display ("sk_live_ab12cd34_…")


def new_api_key() -> NewApiKey:
    prefix = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    return NewApiKey(plaintext=f"{API_KEY_PREFIX}{prefix}_{new_token()}", prefix=prefix)


def parse_api_key(value: str) -> str | None:
    """Return the prefix if ``value`` is a well-formed key, else None (cheap pre-check)."""
    m = _API_KEY_RE.match(value)
    return m.group(1) if m else None
