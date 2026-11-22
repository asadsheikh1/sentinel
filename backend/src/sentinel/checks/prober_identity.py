"""The prober's own integration and ingest token.

The prober has no database access, so the API provisions its identity: one
integration of kind ``prober`` and an ingest token written to a file on a volume
that only the API (write) and the prober (read) mount. On start-up the API keeps
a still-valid token and issues a new one otherwise (the old one is revoked).
"""

import uuid
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sentinel.auth.service import Actor
from sentinel.config import Settings
from sentinel.db.engine import session_scope
from sentinel.db.models import IngestToken, Integration, IntegrationKind
from sentinel.integrations.service import IntegrationService
from sentinel.integrations.tokens import hash_ingest_token, is_well_formed
from sentinel.security.secret_files import read_secret, write_secret

log = structlog.get_logger("sentinel.checks")
PROBER_NAME = "SENTINEL prober"
_ACTOR = Actor.system("prober-provisioning")


async def _valid(
    db: AsyncSession, settings: Settings, integration_id: uuid.UUID, token: str
) -> bool:
    if not is_well_formed(token):
        return False
    token_hash = hash_ingest_token(settings.secret_key.get_secret_value(), token)
    row = await db.scalar(
        select(IngestToken.id).where(
            IngestToken.integration_id == integration_id,
            IngestToken.token_hash == token_hash,
            IngestToken.revoked_at.is_(None),
        )
    )
    return row is not None


async def ensure_prober(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> uuid.UUID | None:
    """Make sure the prober integration exists and its token file is valid."""
    if not settings.prober_token_file:
        return None
    path = Path(settings.prober_token_file)
    async with session_scope(session_factory) as db:
        svc = IntegrationService(db, settings)
        integration = await db.scalar(
            select(Integration).where(Integration.kind == IntegrationKind.PROBER).limit(1)
        )
        if integration is None:
            integration, token = await svc.create(
                name=PROBER_NAME,
                kind=IntegrationKind.PROBER,
                environment=settings.env.value,
                config={},
                expected_signals=["metrics"],
                rate_limit_per_min=None,
                actor=_ACTOR,
                created_by=None,
            )
            write_secret(path, token)
            log.info("prober_provisioned", integration_id=str(integration.id))
            return integration.id
        current = read_secret(path)  # a tiny local file, once at start-up
        if current and await _valid(db, settings, integration.id, current):
            return integration.id
        token = await svc.rotate_token(integration, actor=_ACTOR)
        write_secret(path, token)
        log.info("prober_token_rotated", integration_id=str(integration.id))
        return integration.id
