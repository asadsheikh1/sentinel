"""Password hashing (Argon2id via argon2-cffi, library defaults = RFC 9106 "low memory")."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MAX_PASSWORD_LENGTH = 1024  # bound hashing cost; nobody needs more

_hasher = PasswordHasher()

# Verified against when the email is unknown, so "no such user" costs the same
# time as "wrong password" (prevents user enumeration by timing).
_DUMMY_HASH = _hasher.hash("dummy-password-for-constant-time-checks")


class PasswordPolicyError(ValueError):
    pass


def validate_password(password: str, *, min_length: int) -> None:
    if len(password) < min_length:
        raise PasswordPolicyError(f"Password must be at least {min_length} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"Password must be at most {MAX_PASSWORD_LENGTH} characters.")
    if password.strip() != password or not password.strip():
        raise PasswordPolicyError("Password must not start or end with whitespace.")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Constant-ish time check. ``password_hash=None`` (no password yet) never matches."""
    if len(password) > MAX_PASSWORD_LENGTH:
        return False
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash uses weaker parameters than the current defaults."""
    return _hasher.check_needs_rehash(password_hash)
