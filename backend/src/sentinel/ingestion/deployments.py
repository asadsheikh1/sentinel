"""Deployments: "what changed?" is the first question in an incident.

Every deployment is a ``deployment`` SentinelEvent plus a typed ``deployments``
row (for fast "what was deployed to X before T?" lookups). Idempotent: the
``Idempotency-Key`` header (or, without one, the payload itself) identifies a
deployment, so CI retries don't create duplicates.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.db.models import Deployment, Event, EventType
from sentinel.events.store import record_event
from sentinel.integrations.entities import resolve_entity
from sentinel.security.redaction import redact_text

Name = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,99}$")]
Sha = Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{7,64}$")]
Text200 = Annotated[str, StringConstraints(max_length=200)]
Source = Literal["ci", "script", "rollback", "github"]
Status = Literal["succeeded", "failed", "in_progress"]


class DeploymentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: Name
    version: Text200 | None = None
    image_ref: Annotated[str, StringConstraints(max_length=300)] | None = None
    commit_sha: Sha | None = None
    previous_commit_sha: Sha | None = None
    deployed_at: datetime | None = None
    deployed_by: Text200 | None = None
    environment: Name = "dev"
    source: Source = "ci"
    status: Status = "succeeded"

    @field_validator("commit_sha", "previous_commit_sha")
    @classmethod
    def _lower(cls, value: str | None) -> str | None:
        return value.lower() if value else value

    @field_validator("deployed_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("deployed_at needs a timezone (e.g. 2026-11-22T08:30:00Z)")
        return value


def dedupe_key(data: DeploymentIn, idempotency_key: str | None) -> str:
    if idempotency_key:
        return f"deployment:key:{idempotency_key}"
    canonical = json.dumps(data.model_dump(mode="json"), sort_keys=True)
    return f"deployment:body:{hashlib.sha256(canonical.encode()).hexdigest()}"


async def record_deployment(
    db: AsyncSession,
    data: DeploymentIn,
    *,
    idempotency_key: str | None = None,
    source_integration_id: uuid.UUID | None = None,
    key: str | None = None,
) -> tuple[Deployment, bool]:
    """Store a deployment. Returns (deployment, created)."""
    deployed_at = data.deployed_at or datetime.now(UTC)
    previous = data.previous_commit_sha
    if previous is None:
        previous = await db.scalar(
            select(Deployment.commit_sha)
            .where(
                Deployment.service_name == data.service,
                Deployment.environment == data.environment,
                Deployment.deployed_at < deployed_at,
                Deployment.status == "succeeded",
            )
            .order_by(Deployment.deployed_at.desc())
            .limit(1)
        )
    entity_id = await resolve_entity(
        db, {"service_name": data.service, "deployment_environment_name": data.environment}
    ) or await resolve_entity(db, {"service_name": data.service})
    event_id, created = await record_event(
        db,
        type=EventType.DEPLOYMENT,
        occurred_at=deployed_at,
        dedupe_key=key or dedupe_key(data, idempotency_key),
        source_integration_id=source_integration_id,
        entity_ids=[entity_id] if entity_id else [],
        severity="info" if data.status != "failed" else "warning",
        payload={
            **data.model_dump(mode="json", exclude={"deployed_by"}),
            "deployed_by": redact_text(data.deployed_by).text if data.deployed_by else None,
            "previous_commit_sha": previous,
            "deployed_at": deployed_at.isoformat(),
        },
    )
    existing = await db.scalar(select(Deployment).where(Deployment.event_id == event_id))
    if existing is not None:
        return existing, False
    deployment = Deployment(
        event_id=event_id,
        entity_id=entity_id,
        service_name=data.service,
        environment=data.environment,
        version=data.version,
        image_ref=data.image_ref,
        commit_sha=data.commit_sha,
        previous_commit_sha=previous,
        deployed_at=deployed_at,
        deployed_by=data.deployed_by,
        source=data.source,
        status=data.status,
    )
    db.add(deployment)
    await db.flush()
    return deployment, created


async def update_status(db: AsyncSession, key: str, status: Status) -> Deployment | None:
    """A later status for the same deployment (GitHub sends several per deployment)."""
    event_id = await db.scalar(select(Event.id).where(Event.dedupe_key == key))
    if event_id is None:
        return None
    deployment = await db.scalar(select(Deployment).where(Deployment.event_id == event_id))
    if deployment is not None:
        deployment.status = status
    return deployment
