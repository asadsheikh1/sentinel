"""Ingest tokens: ``sit_<prefix>_<secret>``. Write-only, one integration each."""

import re
import secrets
from dataclasses import dataclass

from sentinel.auth.tokens import hash_token, new_token

_TOKEN_RE = re.compile(r"^sit_([a-z0-9]{8})_([A-Za-z0-9_-]{43})$")
_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_PURPOSE = "ingest"


@dataclass(frozen=True)
class NewIngestToken:
    plaintext: str  # shown to the user exactly once
    prefix: str


def new_ingest_token() -> NewIngestToken:
    prefix = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    return NewIngestToken(plaintext=f"sit_{prefix}_{new_token()}", prefix=prefix)


def is_well_formed(value: str) -> bool:
    return _TOKEN_RE.match(value) is not None


def hash_ingest_token(secret_key: str, token: str) -> str:
    return hash_token(secret_key, token, purpose=_PURPOSE)


def bearer_token(authorization: str | None) -> str | None:
    """Extract a well-formed ingest token from an ``Authorization: Bearer`` header."""
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not is_well_formed(value):
        return None
    return value
