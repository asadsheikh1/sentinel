"""Integration registry: create, update, disable, and the token lifecycle."""

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.auth.service import Actor, ConflictError
from sentinel.config import Settings
from sentinel.db.models import (
    IngestToken,
    Integration,
    IntegrationKind,
    IntegrationSignalStatus,
    IntegrationStatus,
)
from sentinel.integrations.tokens import hash_ingest_token, new_ingest_token
from sentinel.security import audit


def config_hash(config: dict[str, Any]) -> str:
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class IntegrationService:
    def __init__(self, db: AsyncSession, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self._secret = settings.secret_key.get_secret_value()

    async def _audit(
        self,
        actor: Actor,
        action: str,
        integration: Integration,
        details: dict[str, Any] | None = None,
    ) -> None:
        await audit.append(
            self.db,
            actor_type=actor.type,
            actor_id=actor.id,
            action=action,
            target_type="integration",
            target_id=str(integration.id),
            details=details or {},
            ip=actor.ip,
        )

    async def _issue_token(self, integration: Integration, rate_limit_per_min: int) -> str:
        new = new_ingest_token()
        self.db.add(
            IngestToken(
                integration_id=integration.id,
                prefix=new.prefix,
                token_hash=hash_ingest_token(self._secret, new.plaintext),
                rate_limit_per_min=rate_limit_per_min,
            )
        )
        await self.db.flush()
        return new.plaintext

    async def list_all(self) -> Sequence[Integration]:
        return (await self.db.scalars(select(Integration).order_by(Integration.name))).all()

    async def get(self, integration_id: uuid.UUID) -> Integration | None:
        return await self.db.get(Integration, integration_id)

    async def signals(self, integration_id: uuid.UUID) -> Sequence[IntegrationSignalStatus]:
        stmt = (
            select(IntegrationSignalStatus)
            .where(IntegrationSignalStatus.integration_id == integration_id)
            .order_by(IntegrationSignalStatus.signal)
        )
        return (await self.db.scalars(stmt)).all()

    async def active_token(self, integration_id: uuid.UUID) -> IngestToken | None:
        stmt = (
            select(IngestToken)
            .where(IngestToken.integration_id == integration_id, IngestToken.revoked_at.is_(None))
            .order_by(IngestToken.created_at.desc())
            .limit(1)
        )
        return await self.db.scalar(stmt)

    async def create(
        self,
        *,
        name: str,
        kind: IntegrationKind,
        environment: str,
        config: dict[str, Any],
        expected_signals: list[str],
        rate_limit_per_min: int | None,
        actor: Actor,
        created_by: uuid.UUID | None,
    ) -> tuple[Integration, str]:
        """Register an integration. The ingest token is returned once and never again."""
        if await self.db.scalar(select(Integration.id).where(Integration.name == name)):
            raise ConflictError("An integration with this name already exists.")
        integration = Integration(
            name=name,
            kind=kind,
            environment=environment,
            config=config,
            config_hash=config_hash(config),
            expected_signals=sorted(set(expected_signals)),
            status=IntegrationStatus.PENDING,
            status_reason="No telemetry received yet.",
            created_by=created_by,
        )
        self.db.add(integration)
        await self.db.flush()
        limit = rate_limit_per_min or self.settings.ingest_default_rate_limit_per_min
        token = await self._issue_token(integration, limit)
        await self._audit(
            actor, "integration.create", integration, {"name": name, "kind": kind.value}
        )
        return integration, token

    async def update(
        self,
        integration: Integration,
        *,
        actor: Actor,
        name: str | None = None,
        environment: str | None = None,
        config: dict[str, Any] | None = None,
        expected_signals: list[str] | None = None,
    ) -> Integration:
        changed: list[str] = []
        if name is not None and name != integration.name:
            taken = await self.db.scalar(select(Integration.id).where(Integration.name == name))
            if taken:
                raise ConflictError("An integration with this name already exists.")
            integration.name = name
            changed.append("name")
        if environment is not None and environment != integration.environment:
            integration.environment = environment
            changed.append("environment")
        if config is not None and config_hash(config) != integration.config_hash:
            integration.config = config
            integration.config_hash = config_hash(config)
            changed.append("config")
        if expected_signals is not None:
            signals = sorted(set(expected_signals))
            if signals != integration.expected_signals:
                integration.expected_signals = signals
                changed.append("expected_signals")
        if changed:
            await self.db.flush()
            await self._audit(actor, "integration.update", integration, {"changed": changed})
        return integration

    async def _revoke_tokens(self, integration_id: uuid.UUID) -> None:
        await self.db.execute(
            update(IngestToken)
            .where(IngestToken.integration_id == integration_id, IngestToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )

    async def rotate_token(self, integration: Integration, *, actor: Actor) -> str:
        """Replace the token. The old one stops working immediately."""
        if integration.status is IntegrationStatus.DISABLED:
            raise ConflictError("This integration is disabled.")
        current = await self.active_token(integration.id)
        limit = (
            current.rate_limit_per_min
            if current
            else self.settings.ingest_default_rate_limit_per_min
        )
        await self._revoke_tokens(integration.id)
        token = await self._issue_token(integration, limit)
        if integration.status is IntegrationStatus.REVOKED:
            integration.status = IntegrationStatus.PENDING
            integration.status_reason = "No telemetry received since the token was replaced."
        await self._audit(actor, "integration.token_rotate", integration)
        return token

    async def revoke_token(self, integration: Integration, *, actor: Actor) -> None:
        await self._revoke_tokens(integration.id)
        if integration.status is not IntegrationStatus.DISABLED:
            integration.status = IntegrationStatus.REVOKED
            integration.status_reason = "Ingest token revoked."
        await self._audit(actor, "integration.token_revoke", integration)

    async def disable(self, integration: Integration, *, actor: Actor) -> None:
        """Stop accepting data. History and entities are kept."""
        await self._revoke_tokens(integration.id)
        integration.status = IntegrationStatus.DISABLED
        integration.status_reason = "Disabled by an administrator."
        await self._audit(actor, "integration.disable", integration)
