"""Hash-chained, append-only audit log.

Each row stores ``hash = SHA256(prev_hash || canonical_json(row))`` where the
row content is everything except ``id`` (ordering is fixed by the chain itself).
Changing, deleting or reordering any row breaks every later hash, and
``verify_chain`` finds the first broken row.

Writers serialise on a transaction-scoped advisory lock, so two concurrent
requests can never both link to the same ``prev_hash``.
"""

import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.db.models import ActorType, AuditLog

GENESIS_HASH = "0" * 64
_LOCK_KEY = 0x5E7_1A_A0D17  # arbitrary constant: "sentinel audit"
_SENSITIVE = re.compile(r"pass(word)?|secret|token|api[_-]?key|authorization|cookie", re.I)
REDACTED = "[REDACTED]"


def redact(value: Any) -> Any:
    """Recursively replace values under sensitive-looking keys."""
    if isinstance(value, Mapping):
        return {
            str(k): (REDACTED if _SENSITIVE.search(str(k)) else redact(v)) for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()


def compute_hash(
    prev_hash: str,
    *,
    ts: datetime,
    actor_type: str,
    actor_id: str | None,
    action: str,
    target_type: str | None,
    target_id: str | None,
    details: Mapping[str, Any],
    ip: str | None,
) -> str:
    payload = {
        "ts": ts.astimezone(UTC).isoformat(timespec="microseconds"),
        "actor_type": actor_type,
        "actor_id": actor_id,
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "details": details,
        "ip": ip,
    }
    return hashlib.sha256(prev_hash.encode() + _canonical(payload)).hexdigest()


async def append(
    session: AsyncSession,
    *,
    actor_type: ActorType,
    action: str,
    actor_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    details: Mapping[str, Any] | None = None,
    ip: str | None = None,
) -> AuditLog:
    """Append one event in the caller's transaction (committed with the change it records)."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
    prev = await session.scalar(select(AuditLog.hash).order_by(AuditLog.id.desc()).limit(1))
    prev_hash = prev or GENESIS_HASH

    # Canonical forms, so what we hash is exactly what Postgres will return later.
    ip = str(ipaddress.ip_address(ip)) if ip else None
    clean_details: dict[str, Any] = json.loads(_canonical(redact(dict(details or {}))))
    ts = datetime.now(UTC)
    row = AuditLog(
        ts=ts,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=clean_details,
        ip=ip,
        prev_hash=prev_hash,
        hash=compute_hash(
            prev_hash,
            ts=ts,
            actor_type=actor_type.value,
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=clean_details,
            ip=ip,
        ),
    )
    session.add(row)
    await session.flush()
    return row


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    checked: int
    total: int
    first_bad_id: int | None = None
    reason: str | None = None


async def verify_chain(session: AsyncSession, *, batch: int = 1000) -> ChainReport:
    """Recompute every hash in id order. Stops at the first inconsistency."""
    total = int(await session.scalar(select(func.count()).select_from(AuditLog)) or 0)
    expected_prev = GENESIS_HASH
    checked = 0
    last_id = 0
    while True:
        rows = (
            await session.scalars(
                select(AuditLog).where(AuditLog.id > last_id).order_by(AuditLog.id).limit(batch)
            )
        ).all()
        if not rows:
            return ChainReport(ok=True, checked=checked, total=total)
        for row in rows:
            if row.prev_hash != expected_prev:
                return ChainReport(False, checked, total, row.id, "prev_hash does not link")
            recomputed = compute_hash(
                row.prev_hash,
                ts=row.ts,
                actor_type=row.actor_type.value,
                actor_id=row.actor_id,
                action=row.action,
                target_type=row.target_type,
                target_id=row.target_id,
                details=row.details,
                ip=str(row.ip) if row.ip is not None else None,
            )
            if recomputed != row.hash:
                return ChainReport(False, checked, total, row.id, "content does not match hash")
            expected_prev = row.hash
            checked += 1
            last_id = row.id
