"""Incidents: list, detail, status and severity changes, timeline, comments."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from sentinel.api.deps import DbSession
from sentinel.api.routers.ingest import SignalOut
from sentinel.api.security import actor_for, require
from sentinel.auth.permissions import Permission
from sentinel.auth.service import SessionPrincipal
from sentinel.db.models import Deployment, Incident, IncidentStatus, Severity
from sentinel.detection.signals import as_dict
from sentinel.incidents import service
from sentinel.incidents.lifecycle import InvalidTransitionError
from sentinel.security import audit

router = APIRouter(prefix="/api/v1/incidents", tags=["incidents"])
Viewer = Annotated[SessionPrincipal, Depends(require(Permission.INCIDENTS_READ))]
Responder = Annotated[SessionPrincipal, Depends(require(Permission.INCIDENTS_WRITE))]


class IncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    number: int
    ref: str
    title: str
    status: IncidentStatus
    severity: Severity
    service_name: str | None
    primary_entity_id: uuid.UUID | None
    suspected_deployment_id: uuid.UUID | None
    started_at: datetime
    detected_at: datetime
    last_signal_at: datetime
    mitigated_at: datetime | None
    resolved_at: datetime | None
    closed_at: datetime | None
    created_by: str
    updated_at: datetime


class EntityRef(BaseModel):
    id: uuid.UUID
    type: str
    key: str
    display_name: str
    role: str


class AttachedSignal(SignalOut):
    reason: str


class DeploymentRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    service_name: str
    version: str | None
    commit_sha: str | None
    previous_commit_sha: str | None
    deployed_at: datetime
    source: str


class IncidentDetail(IncidentOut):
    summary: str | None
    entities: list[EntityRef]
    signals: list[AttachedSignal]
    suspected_deployment: DeploymentRef | None
    investigation: None = None  # the investigator arrives in Step 17


class TimelineEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ts: datetime
    type: str
    actor: str
    data: dict[str, Any]


class IncidentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: IncidentStatus | None = None
    severity: Severity | None = None
    title: str | None = Field(default=None, min_length=1, max_length=300)
    note: str | None = Field(default=None, max_length=service.MAX_COMMENT)


class CommentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=service.MAX_COMMENT)


def _out(incident: Incident) -> IncidentOut:
    return IncidentOut.model_validate({**_fields(incident), "ref": incident.ref})


def _fields(incident: Incident) -> dict[str, Any]:
    return {c: getattr(incident, c) for c in IncidentOut.model_fields if c != "ref"}


async def _get(db: DbSession, incident_id: uuid.UUID) -> Incident:
    incident = await db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found.")
    return incident


@router.get("", response_model=list[IncidentOut])
async def list_incidents(
    db: DbSession,
    _: Viewer,
    status_: Annotated[list[IncidentStatus] | None, Query(alias="status")] = None,
    severity: Annotated[list[Severity] | None, Query()] = None,
    service_name: Annotated[str | None, Query(alias="service", max_length=100)] = None,
    cursor: Annotated[int | None, Query(ge=1, description="the last number seen")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[IncidentOut]:
    """Newest first. Page with ``cursor`` = the last ``number`` you got."""
    stmt = select(Incident).order_by(Incident.number.desc()).limit(limit)
    if status_:
        stmt = stmt.where(Incident.status.in_(status_))
    if severity:
        stmt = stmt.where(Incident.severity.in_(severity))
    if service_name is not None:
        stmt = stmt.where(Incident.service_name == service_name)
    if cursor is not None:
        stmt = stmt.where(Incident.number < cursor)
    return [_out(i) for i in (await db.scalars(stmt)).all()]


@router.get("/{incident_id}", response_model=IncidentDetail)
async def get_incident(incident_id: uuid.UUID, db: DbSession, _: Viewer) -> IncidentDetail:
    incident = await _get(db, incident_id)
    deployment = (
        await db.get(Deployment, incident.suspected_deployment_id)
        if incident.suspected_deployment_id
        else None
    )
    return IncidentDetail(
        **_out(incident).model_dump(),
        summary=incident.summary,
        entities=[
            EntityRef(id=e.id, type=e.type.value, key=e.key, display_name=e.display_name, role=r)
            for e, r in await service.entities_of(db, incident.id)
        ],
        signals=[
            AttachedSignal(**as_dict(s), reason=r)
            for s, r in await service.signals_of(db, incident.id)
        ],
        suspected_deployment=DeploymentRef.model_validate(deployment) if deployment else None,
    )


@router.patch("/{incident_id}", response_model=IncidentOut)
async def update_incident(
    incident_id: uuid.UUID, body: IncidentPatch, request: Request, db: DbSession, who: Responder
) -> IncidentOut:
    """Change status (state machine), severity (any direction) or title."""
    incident = await _get(db, incident_id)
    actor = service.user_actor(who.user.id)
    before = {"status": incident.status.value, "severity": incident.severity.value}
    try:
        if body.status is not None:
            await service.transition(db, incident, body.status, actor=actor, note=body.note)
    except InvalidTransitionError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    if body.severity is not None:
        await service.set_severity(db, incident, body.severity, actor=actor)
    if body.title is not None:
        await service.set_title(db, incident, body.title, actor=actor)
    who_ = actor_for(request, who)
    await audit.append(
        db,
        actor_type=who_.type,
        actor_id=who_.id,
        action="incident.updated",
        target_type="incident",
        target_id=str(incident.id),
        details={
            "before": before,
            "after": {"status": incident.status.value, "severity": incident.severity.value},
            "title_changed": body.title is not None,
        },
        ip=who_.ip,
    )
    return _out(incident)


@router.get("/{incident_id}/timeline", response_model=list[TimelineEventOut])
async def get_timeline(incident_id: uuid.UUID, db: DbSession, _: Viewer) -> list[TimelineEventOut]:
    """Every event, oldest first."""
    incident = await _get(db, incident_id)
    return [TimelineEventOut.model_validate(e) for e in await service.timeline(db, incident.id)]


@router.post("/{incident_id}/comments", response_model=TimelineEventOut, status_code=201)
async def add_comment(
    incident_id: uuid.UUID, body: CommentIn, db: DbSession, who: Responder
) -> TimelineEventOut:
    incident = await _get(db, incident_id)
    event = await service.comment(db, incident, body.text, actor=service.user_actor(who.user.id))
    return TimelineEventOut.model_validate(event)
