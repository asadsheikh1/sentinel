"""Machine ingestion (Alertmanager, deployments, GitHub) and the detection read API."""

import json
import re
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select

from sentinel.api.deps import DbSession
from sentinel.api.security import github_signature, require, require_scope
from sentinel.auth.permissions import KeyScope, Permission
from sentinel.auth.service import KeyPrincipal, SessionPrincipal
from sentinel.db.models import Deployment, SignalKind, SignalStatus
from sentinel.detection.signals import (
    AlertmanagerWebhook,
    as_dict,
    ingest_alertmanager,
    list_signals,
)
from sentinel.ingestion import github
from sentinel.ingestion.deployments import DeploymentIn, record_deployment

router = APIRouter(prefix="/api/v1/ingest", tags=["ingest"])
signals_router = APIRouter(prefix="/api/v1/signals", tags=["signals"])
deployments_router = APIRouter(prefix="/api/v1/deployments", tags=["deployments"])

Viewer = Annotated[SessionPrincipal, Depends(require(Permission.INCIDENTS_READ))]
AlertsKey = Annotated[KeyPrincipal, Depends(require_scope(KeyScope.ALERTS_WRITE))]
DeploymentsKey = Annotated[KeyPrincipal, Depends(require_scope(KeyScope.DEPLOYMENTS_WRITE))]
GitHubBody = Annotated[bytes, Depends(github_signature())]
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_DELIVERY = re.compile(r"^[A-Za-z0-9-]{1,100}$")


class IngestResult(BaseModel):
    created: int
    updated: int
    unchanged: int


class DeploymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_id: uuid.UUID
    entity_id: uuid.UUID | None
    service_name: str
    environment: str
    version: str | None
    image_ref: str | None
    commit_sha: str | None
    previous_commit_sha: str | None
    deployed_at: datetime
    deployed_by: str | None
    source: str
    status: str


class SignalOut(BaseModel):
    id: uuid.UUID
    kind: SignalKind
    status: SignalStatus
    severity: str
    title: str
    entity_id: uuid.UUID | None
    service_name: str | None
    source_integration_id: uuid.UUID | None
    labels: dict[str, Any]
    payload: dict[str, Any]
    starts_at: datetime
    ends_at: datetime | None
    received_at: datetime


class GitHubResult(BaseModel):
    event: str
    result: str


@router.post("/alertmanager", response_model=IngestResult)
async def alertmanager_webhook(
    body: AlertmanagerWebhook, db: DbSession, _: AlertsKey
) -> IngestResult:
    """Alertmanager webhook (v4). Idempotent: re-deliveries update, never duplicate."""
    return IngestResult(**await ingest_alertmanager(db, body))


@router.post("/deployments", response_model=DeploymentOut)
async def deployment_webhook(
    body: DeploymentIn,
    response: Response,
    db: DbSession,
    _: DeploymentsKey,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> DeploymentOut:
    """A deployment from any CI or script. 201 when new, 200 for a repeat."""
    if idempotency_key is not None and not _IDEMPOTENCY.match(idempotency_key):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid Idempotency-Key.")
    deployment, created = await record_deployment(db, body, idempotency_key=idempotency_key)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return DeploymentOut.model_validate(deployment)


@router.post("/github", response_model=GitHubResult, status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    raw: GitHubBody,
    db: DbSession,
    event: Annotated[str, Header(alias="X-GitHub-Event")],
    delivery: Annotated[str, Header(alias="X-GitHub-Delivery")],
) -> GitHubResult:
    """Signed GitHub webhook: deployment_status, push and workflow_run become events."""
    if not _DELIVERY.match(delivery) or not re.fullmatch(r"[a-z_]{1,50}", event):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid GitHub headers.")
    try:
        body = json.loads(raw)
        result = await github.handle(db, event, delivery, body)
    except (ValueError, KeyError, TypeError, ValidationError) as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unexpected payload.") from e
    return GitHubResult(event=event, result=result)


@signals_router.get("", response_model=list[SignalOut])
async def get_signals(
    db: DbSession,
    _: Viewer,
    status_: Annotated[SignalStatus | None, Query(alias="status")] = None,
    kind: SignalKind | None = None,
    service: Annotated[str | None, Query(max_length=100)] = None,
    entity: uuid.UUID | None = None,
    since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[SignalOut]:
    """Newest first."""
    rows = await list_signals(
        db, status=status_, kind=kind, service=service, entity=entity, since=since, limit=limit
    )
    return [SignalOut(**as_dict(s)) for s in rows]


@deployments_router.get("", response_model=list[DeploymentOut])
async def get_deployments(
    db: DbSession,
    _: Viewer,
    service: Annotated[str | None, Query(max_length=100)] = None,
    since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[DeploymentOut]:
    """Newest first."""
    stmt = select(Deployment).order_by(Deployment.deployed_at.desc()).limit(limit)
    if service is not None:
        stmt = stmt.where(Deployment.service_name == service)
    if since is not None:
        stmt = stmt.where(Deployment.deployed_at >= since)
    return [DeploymentOut.model_validate(d) for d in (await db.scalars(stmt)).all()]
