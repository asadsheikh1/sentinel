"""Webhook signatures (GitHub style: ``sha256=<hex HMAC-SHA256 of the raw body>``)."""

import hashlib
import hmac


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def signature_valid(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time comparison; any missing or malformed header is invalid."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    return hmac.compare_digest(sign(secret, body), header)
