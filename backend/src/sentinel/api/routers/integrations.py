"""/api/v1/integrations and /api/v1/entities: telemetry sources and what they reveal."""

import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import or_, select

from sentinel.api.deps import DbSession
from sentinel.api.security import actor_for, ingest_token, require
from sentinel.auth.permissions import Permission
from sentinel.auth.service import ConflictError, SessionPrincipal
from sentinel.db.models import (
    Entity,
    EntityRelation,
    EntityType,
    Integration,
    IntegrationKind,
    IntegrationStatus,
)
from sentinel.integrations import agent_config
from sentinel.integrations.agent_config import AgentConfigError, AgentMode
from sentinel.integrations.gate import TokenInfo
from sentinel.integrations.service import IntegrationService

router = APIRouter(prefix="/api/v1/integrations", tags=["integrations"])
entities_router = APIRouter(prefix="/api/v1/entities", tags=["entities"])

Manager = Annotated[SessionPrincipal, Depends(require(Permission.INTEGRATIONS_MANAGE))]
Viewer = Annotated[SessionPrincipal, Depends(require(Permission.INCIDENTS_READ))]
Signal = Literal["metrics", "logs", "traces", "events"]
Agent = Annotated[TokenInfo, Depends(ingest_token())]


class IntegrationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: IntegrationKind
    environment: str = Field(default="dev", min_length=1, max_length=50)
    config: dict[str, Any] = Field(default_factory=dict)
    expected_signals: list[Signal] = Field(default_factory=list)
    rate_limit_per_min: int | None = Field(default=None, ge=1, le=100_000)


class IntegrationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    environment: str | None = Field(default=None, min_length=1, max_length=50)
    config: dict[str, Any] | None = None
    expected_signals: list[Signal] | None = None


class IntegrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    kind: IntegrationKind
    environment: str
    config: dict[str, Any]
    expected_signals: list[str]
    status: IntegrationStatus
    status_reason: str | None
    created_at: datetime


class IntegrationCreated(BaseModel):
    integration: IntegrationOut
    ingest_token: str = Field(description="Shown once. Store it in your secret manager now.")


class TokenOut(BaseModel):
    ingest_token: str = Field(description="Shown once. The previous token no longer works.")


class SignalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    signal: str
    last_seen_at: datetime
    last_count_window: int
    last_error: str | None


class StatusOut(BaseModel):
    id: uuid.UUID
    status: IntegrationStatus
    status_reason: str | None
    expected_signals: list[str]
    signals: list[SignalOut]


class EntityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: EntityType
    key: str
    display_name: str
    attributes: dict[str, Any]
    integration_ids: list[uuid.UUID]
    first_seen: datetime
    last_seen: datetime


class RelationOut(BaseModel):
    from_entity_id: uuid.UUID
    to_entity_id: uuid.UUID
    relation: str
    source: str
    confidence: float
    last_seen: datetime


class AgentSetupOut(BaseModel):
    integration_id: uuid.UUID
    kind: IntegrationKind
    endpoint: str
    agent_version: str
    config_hash: str
    install_command: str = Field(description="Linux host (systemd). Asks for the ingest token.")
    container_command: str = Field(description="Podman (or Docker) agent container.")
    uninstall_command: str
    notes: list[str]


def _service(request: Request, db: DbSession) -> IntegrationService:
    return IntegrationService(db, request.app.state.settings)


Service = Annotated[IntegrationService, Depends(_service)]


def _check_agent_options(kind: IntegrationKind, config: dict[str, Any]) -> None:
    """Agent options are validated before they are stored, not only when rendered."""
    try:
        agent_config.validate_options(kind, config)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()
        )
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"Invalid agent options: {problems}"
        ) from e


async def _get(svc: IntegrationService, integration_id: uuid.UUID) -> Integration:
    integration = await svc.get(integration_id)
    if integration is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Integration not found.")
    return integration


async def _commit_and_forget(request: Request, db: DbSession, integration_id: uuid.UUID) -> None:
    """Commit first, so a concurrent request can't re-cache the old token."""
    await db.commit()
    request.app.state.ingest_gate.invalidate(integration_id)


@router.get("", response_model=list[IntegrationOut])
async def list_integrations(svc: Service, _: Viewer) -> list[IntegrationOut]:
    return [IntegrationOut.model_validate(i) for i in await svc.list_all()]


@router.post("", response_model=IntegrationCreated, status_code=status.HTTP_201_CREATED)
async def create_integration(
    body: IntegrationCreate, request: Request, db: DbSession, svc: Service, admin: Manager
) -> IntegrationCreated:
    _check_agent_options(body.kind, body.config)
    try:
        integration, token = await svc.create(
            name=body.name,
            kind=body.kind,
            environment=body.environment,
            config=body.config,
            expected_signals=list(body.expected_signals),
            rate_limit_per_min=body.rate_limit_per_min,
            actor=actor_for(request, admin),
            created_by=admin.user.id,
        )
    except ConflictError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    # Commit before forgetting cached "unknown token" answers: otherwise a client
    # using the new token at once can be rejected, and that cached for 5 s.
    await db.commit()
    request.app.state.ingest_gate.clear_negative_cache()
    return IntegrationCreated(
        integration=IntegrationOut.model_validate(integration), ingest_token=token
    )


@router.get("/{integration_id}", response_model=IntegrationOut)
async def get_integration(integration_id: uuid.UUID, svc: Service, _: Manager) -> IntegrationOut:
    return IntegrationOut.model_validate(await _get(svc, integration_id))


@router.patch("/{integration_id}", response_model=IntegrationOut)
async def update_integration(
    integration_id: uuid.UUID,
    body: IntegrationUpdate,
    request: Request,
    svc: Service,
    admin: Manager,
) -> IntegrationOut:
    integration = await _get(svc, integration_id)
    if body.config is not None:
        _check_agent_options(integration.kind, body.config)
    try:
        await svc.update(
            integration,
            actor=actor_for(request, admin),
            name=body.name,
            environment=body.environment,
            config=body.config,
            expected_signals=list(body.expected_signals)
            if body.expected_signals is not None
            else None,
        )
    except ConflictError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    return IntegrationOut.model_validate(integration)


@router.delete("/{integration_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disable_integration(
    integration_id: uuid.UUID, request: Request, db: DbSession, svc: Service, admin: Manager
) -> Response:
    """Disable the integration and revoke its token. History is kept."""
    await svc.disable(await _get(svc, integration_id), actor=actor_for(request, admin))
    await _commit_and_forget(request, db, integration_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{integration_id}/tokens/rotate", response_model=TokenOut)
async def rotate_token(
    integration_id: uuid.UUID, request: Request, db: DbSession, svc: Service, admin: Manager
) -> TokenOut:
    integration = await _get(svc, integration_id)
    try:
        token = await svc.rotate_token(integration, actor=actor_for(request, admin))
    except ConflictError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    await _commit_and_forget(request, db, integration_id)
    request.app.state.ingest_gate.clear_negative_cache()
    return TokenOut(ingest_token=token)


@router.post("/{integration_id}/tokens/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_token(
    integration_id: uuid.UUID, request: Request, db: DbSession, svc: Service, admin: Manager
) -> Response:
    await svc.revoke_token(await _get(svc, integration_id), actor=actor_for(request, admin))
    await _commit_and_forget(request, db, integration_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{integration_id}/status", response_model=StatusOut)
async def integration_status(integration_id: uuid.UUID, svc: Service, _: Viewer) -> StatusOut:
    integration = await _get(svc, integration_id)
    signals = await svc.signals(integration_id)
    return StatusOut(
        id=integration.id,
        status=integration.status,
        status_reason=integration.status_reason,
        expected_signals=integration.expected_signals,
        signals=[SignalOut.model_validate(s) for s in signals],
    )


def _agent_dir(request: Request) -> Path:
    configured = request.app.state.settings.agent_dir
    return Path(configured) if configured else agent_config.default_agent_dir()


@router.get(
    "/{integration_id}/agent-config",
    response_class=PlainTextResponse,
    responses={200: {"content": {"application/yaml": {}}}},
)
async def agent_config_file(
    integration_id: uuid.UUID,
    request: Request,
    svc: Service,
    _: Agent,
    mode: AgentMode = AgentMode.HOST,
) -> PlainTextResponse:
    """The rendered Collector config for this integration's agent (ingest token auth)."""
    integration = await _get(svc, integration_id)
    try:
        rendered = agent_config.render(
            kind=integration.kind,
            environment=integration.environment,
            config=integration.config,
            mode=mode,
            agent_dir=_agent_dir(request),
        )
    except AgentConfigError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    return PlainTextResponse(
        rendered.text,
        media_type="application/yaml",
        headers={"X-Sentinel-Config-Hash": rendered.config_hash, "Cache-Control": "no-store"},
    )


@router.get("/{integration_id}/setup", response_model=AgentSetupOut)
async def agent_setup(
    integration_id: uuid.UUID, request: Request, svc: Service, _: Manager
) -> AgentSetupOut:
    """Install instructions for an agent integration. The token is never included."""
    integration = await _get(svc, integration_id)
    try:
        rendered = agent_config.render(
            kind=integration.kind,
            environment=integration.environment,
            config=integration.config,
            agent_dir=_agent_dir(request),
        )
    except AgentConfigError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    endpoint = request.app.state.settings.agent_public_endpoint
    containers = agent_config.collects_containers(integration.kind, rendered.options)
    notes = [
        "The installer asks for the ingest token (it is not stored in shell history).",
        "Supported: Ubuntu 22.04/24.04, Debian 12/13, Amazon Linux 2023 (x86_64, arm64).",
        "The agent only sends data. It opens no ports except loopback 4317/4318 for "
        "local apps and 18888 for its own metrics.",
    ]
    if containers:
        notes.append(
            "Container stats go through a GET-only socket proxy (HAProxy). The installer "
            f"uses the {rendered.options.container_engine} socket; pass --engine-socket "
            "to use another one."
        )
    return AgentSetupOut(
        integration_id=integration.id,
        kind=integration.kind,
        endpoint=endpoint,
        agent_version=agent_config.AGENT_VERSION,
        config_hash=rendered.config_hash,
        install_command=agent_config.install_command(endpoint, integration.id),
        container_command=agent_config.container_command(endpoint, integration.id),
        uninstall_command=(
            f"curl -fsSL {endpoint}/agent/uninstall.sh -o sentinel-agent-uninstall.sh && "
            "sudo bash sentinel-agent-uninstall.sh"
        ),
        notes=notes,
    )


@entities_router.get("", response_model=list[EntityOut])
async def list_entities(
    db: DbSession,
    _: Viewer,
    type: EntityType | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[EntityOut]:
    stmt = select(Entity).order_by(Entity.type, Entity.key).limit(limit)
    if type is not None:
        stmt = stmt.where(Entity.type == type)
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        stmt = stmt.where(
            or_(
                Entity.key.ilike(pattern, escape="\\"),
                Entity.display_name.ilike(pattern, escape="\\"),
            )
        )
    return [EntityOut.model_validate(e) for e in (await db.scalars(stmt)).all()]


@entities_router.get("/{entity_id}", response_model=EntityOut)
async def get_entity(entity_id: uuid.UUID, db: DbSession, _: Viewer) -> EntityOut:
    entity = await db.get(Entity, entity_id)
    if entity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entity not found.")
    return EntityOut.model_validate(entity)


@entities_router.get("/{entity_id}/relations", response_model=list[RelationOut])
async def entity_relations(entity_id: uuid.UUID, db: DbSession, _: Viewer) -> list[RelationOut]:
    if await db.get(Entity, entity_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entity not found.")
    stmt = select(EntityRelation).where(
        or_(EntityRelation.from_entity_id == entity_id, EntityRelation.to_entity_id == entity_id)
    )
    return [
        RelationOut(
            from_entity_id=r.from_entity_id,
            to_entity_id=r.to_entity_id,
            relation=r.relation,
            source=r.source,
            confidence=r.confidence,
            last_seen=r.last_seen,
        )
        for r in (await db.scalars(stmt)).all()
    ]
