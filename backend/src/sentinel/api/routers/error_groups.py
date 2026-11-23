"""Error groups (log fingerprints): list and detail, for any signed-in role."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from sentinel.api.deps import DbSession
from sentinel.api.routers.ingest import SignalOut
from sentinel.api.security import require
from sentinel.auth.permissions import Permission
from sentinel.auth.service import SessionPrincipal
from sentinel.db.models import ErrorGroup, Signal, SignalKind
from sentinel.detection.signals import as_dict

router = APIRouter(prefix="/api/v1/error-groups", tags=["error-groups"])
Viewer = Annotated[SessionPrincipal, Depends(require(Permission.INCIDENTS_READ))]


class ErrorGroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    scope_key: str
    entity_id: uuid.UUID | None
    service_name: str | None
    fingerprint: str
    exception_type: str
    normalized_message: str
    top_frame: str
    level: str
    first_seen: datetime
    last_seen: datetime
    total_count: int
    sample_trace_ids: list[str]


class ErrorGroupDetail(ErrorGroupOut):
    sample_log: dict[str, Any]
    minute_counts: dict[str, int]
    signals: list[SignalOut]


@router.get("", response_model=list[ErrorGroupOut])
async def list_error_groups(
    db: DbSession,
    _: Viewer,
    service: Annotated[str | None, Query(max_length=100)] = None,
    entity: uuid.UUID | None = None,
    since: Annotated[datetime | None, Query(description="first seen at or after")] = None,
    seen_since: Annotated[datetime | None, Query(description="last seen at or after")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[ErrorGroupOut]:
    """Most recently seen first."""
    stmt = select(ErrorGroup).order_by(ErrorGroup.last_seen.desc()).limit(limit)
    if service is not None:
        stmt = stmt.where(ErrorGroup.service_name == service)
    if entity is not None:
        stmt = stmt.where(ErrorGroup.entity_id == entity)
    if since is not None:
        stmt = stmt.where(ErrorGroup.first_seen >= since)
    if seen_since is not None:
        stmt = stmt.where(ErrorGroup.last_seen >= seen_since)
    return [ErrorGroupOut.model_validate(g) for g in (await db.scalars(stmt)).all()]


@router.get("/{group_id}", response_model=ErrorGroupDetail)
async def get_error_group(group_id: uuid.UUID, db: DbSession, _: Viewer) -> ErrorGroupDetail:
    group = await db.get(ErrorGroup, group_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Error group not found.")
    signals = (
        await db.scalars(
            select(Signal)
            .where(
                Signal.kind.in_([SignalKind.ERROR_GROUP_NEW, SignalKind.ERROR_GROUP_SPIKE]),
                Signal.labels["error_group_id"].astext == str(group.id),
            )
            .order_by(Signal.starts_at.desc())
            .limit(50)
        )
    ).all()
    base = ErrorGroupOut.model_validate(group).model_dump()
    return ErrorGroupDetail(
        **base,
        sample_log=group.sample_log,
        minute_counts=group.minute_counts,
        signals=[SignalOut(**as_dict(s)) for s in signals],
    )
