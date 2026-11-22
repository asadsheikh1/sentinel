"""/api/v1/checks (health checks), /api/v1/prober/checks (the prober's work list),
and /api/v1/events (the SentinelEvent stream)."""

import hashlib
import json
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from sentinel.api.deps import DbSession
from sentinel.api.security import actor_for, prober_token, require
from sentinel.auth.permissions import Permission
from sentinel.auth.service import ConflictError, SessionPrincipal
from sentinel.checks.service import CheckError, CheckInput, CheckService
from sentinel.db.models import CheckKind, Event, EventType, HealthCheck, ProbeState
from sentinel.integrations.gate import TokenInfo

router = APIRouter(prefix="/api/v1/checks", tags=["checks"])
prober_router = APIRouter(prefix="/api/v1/prober", tags=["prober"])
events_router = APIRouter(prefix="/api/v1/events", tags=["events"])

Manager = Annotated[SessionPrincipal, Depends(require(Permission.INTEGRATIONS_MANAGE))]
Viewer = Annotated[SessionPrincipal, Depends(require(Permission.INCIDENTS_READ))]
Prober = Annotated[TokenInfo, Depends(prober_token())]


class CheckOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    integration_id: uuid.UUID
    name: str
    kind: CheckKind
    target: str
    interval_s: int
    timeout_s: float
    assertions: dict[str, Any]
    enabled: bool
    entity_id: uuid.UUID | None
    state: ProbeState
    state_changed_at: datetime | None
    last_result_at: datetime | None
    created_at: datetime


class ProberCheck(BaseModel):
    id: uuid.UUID
    integration_id: uuid.UUID
    name: str
    kind: CheckKind
    target: str
    interval_s: int
    timeout_s: float
    assertions: dict[str, Any]


class ProberConfig(BaseModel):
    version: str = Field(description="Changes whenever the list changes.")
    checks: list[ProberCheck]


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: EventType
    source_integration_id: uuid.UUID | None
    entity_ids: list[uuid.UUID]
    occurred_at: datetime
    received_at: datetime
    severity: str | None
    payload: dict[str, Any]


def _service(request: Request, db: DbSession) -> CheckService:
    return CheckService(db, request.app.state.settings, request.app.state.check_guard)


Service = Annotated[CheckService, Depends(_service)]


def _parse(body: dict[str, Any]) -> CheckInput:
    try:
        return CheckInput.model_validate(body)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'body'}: {err['msg']}" for err in e.errors()
        )
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, problems) from e


async def _get(svc: CheckService, check_id: uuid.UUID) -> HealthCheck:
    check = await svc.get(check_id)
    if check is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Check not found.")
    return check


@router.get("", response_model=list[CheckOut])
async def list_checks(svc: Service, _: Manager) -> list[CheckOut]:
    return [CheckOut.model_validate(c) for c in await svc.list_all()]


@router.post("", response_model=CheckOut, status_code=status.HTTP_201_CREATED)
async def create_check(
    body: dict[str, Any], request: Request, svc: Service, admin: Manager
) -> CheckOut:
    """Create a check. The target is SSRF-validated now and before every run."""
    data = _parse(body)
    try:
        check = await svc.create(data, actor=actor_for(request, admin), created_by=admin.user.id)
    except CheckError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    except ConflictError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    return CheckOut.model_validate(check)


@router.get("/{check_id}", response_model=CheckOut)
async def get_check(check_id: uuid.UUID, svc: Service, _: Manager) -> CheckOut:
    return CheckOut.model_validate(await _get(svc, check_id))


@router.patch("/{check_id}", response_model=CheckOut)
async def update_check(
    check_id: uuid.UUID, body: dict[str, Any], request: Request, svc: Service, admin: Manager
) -> CheckOut:
    """Change a check. Send the fields to change; the result is validated as a whole."""
    check = await _get(svc, check_id)
    current = {
        "integration_id": str(check.integration_id),
        "name": check.name,
        "kind": check.kind.value,
        "target": check.target,
        "interval_s": check.interval_s,
        "timeout_s": check.timeout_s,
        "assertions": check.assertions,
        "enabled": check.enabled,
    }
    if "kind" in body and body["kind"] != check.kind.value and "assertions" not in body:
        current["assertions"] = {}  # assertions are per kind
    data = _parse({**current, **body})
    try:
        await svc.update(check, data, actor=actor_for(request, admin))
    except CheckError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    return CheckOut.model_validate(check)


@router.delete("/{check_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_check(
    check_id: uuid.UUID, request: Request, svc: Service, admin: Manager
) -> Response:
    await svc.delete(await _get(svc, check_id), actor=actor_for(request, admin))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@prober_router.get("/checks", response_model=ProberConfig)
async def prober_checks(svc: Service, _: Prober) -> ProberConfig:
    """The prober's work list (prober token only)."""
    checks = [
        ProberCheck(
            id=c.id,
            integration_id=c.integration_id,
            name=c.name,
            kind=c.kind,
            target=c.target,
            interval_s=c.interval_s,
            timeout_s=c.timeout_s,
            assertions=c.assertions,
        )
        for c in await svc.for_prober()
    ]
    canonical = json.dumps([c.model_dump(mode="json") for c in checks], sort_keys=True)
    return ProberConfig(version=hashlib.sha256(canonical.encode()).hexdigest()[:16], checks=checks)


@events_router.get("", response_model=list[EventOut])
async def list_events(
    db: DbSession,
    _: Viewer,
    type: EventType | None = None,
    entity: uuid.UUID | None = None,
    since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[EventOut]:
    """Newest first."""
    stmt = select(Event).order_by(Event.occurred_at.desc()).limit(limit)
    if type is not None:
        stmt = stmt.where(Event.type == type)
    if entity is not None:
        stmt = stmt.where(Event.entity_ids.contains([entity]))
    if since is not None:
        stmt = stmt.where(Event.occurred_at >= since)
    return [EventOut.model_validate(e) for e in (await db.scalars(stmt)).all()]
