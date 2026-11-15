"""/api/v1/audit: browse and verify the hash-chained audit log."""

import base64
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from sentinel.api.deps import DbSession
from sentinel.api.schemas import AuditEntryOut, AuditPage, AuditVerifyOut
from sentinel.api.security import require
from sentinel.auth.permissions import Permission
from sentinel.auth.service import SessionPrincipal
from sentinel.db.models import AuditLog
from sentinel.security import audit

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])
Auditor = Annotated[SessionPrincipal, Depends(require(Permission.AUDIT_READ))]


def _encode_cursor(last_id: int) -> str:
    return base64.urlsafe_b64encode(str(last_id).encode()).decode()


def _decode_cursor(cursor: str) -> int:
    try:
        return int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid cursor.") from e


@router.get("", response_model=AuditPage)
async def list_audit(
    db: DbSession,
    _: Auditor,
    actor: str | None = None,
    action: str | None = None,
    since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> AuditPage:
    """Newest first. ``action`` matches a prefix (``user.`` → all user events)."""
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit + 1)
    if cursor:
        stmt = stmt.where(AuditLog.id < _decode_cursor(cursor))
    if actor:
        stmt = stmt.where(AuditLog.actor_id == actor)
    if action:
        escaped = action.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(AuditLog.action.like(f"{escaped}%", escape="\\"))
    if since:
        stmt = stmt.where(AuditLog.ts >= since)
    rows = list((await db.scalars(stmt)).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [AuditEntryOut.model_validate(r) for r in rows]
    return AuditPage(
        items=items, next_cursor=_encode_cursor(rows[-1].id) if has_more and rows else None
    )


@router.get("/verify", response_model=AuditVerifyOut)
async def verify(db: DbSession, _: Auditor) -> AuditVerifyOut:
    """Recompute the whole hash chain. ``ok: false`` means a row was altered or removed."""
    report = await audit.verify_chain(db)
    return AuditVerifyOut(
        ok=report.ok,
        checked=report.checked,
        total=report.total,
        first_bad_id=report.first_bad_id,
        reason=report.reason,
    )
