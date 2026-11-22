"""Alertmanager's API key (scope ``alerts:write`` only), provisioned by the API.

Alertmanager sends rule-pack alerts to ``/api/v1/ingest/alertmanager`` with
this key. The API writes it to a volume that only Alertmanager mounts (read
only); Alertmanager reads the file on every delivery. On start-up a still-valid
key is kept; otherwise the old system keys are revoked and a new one is issued.
"""

from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sentinel.auth.permissions import KeyScope
from sentinel.auth.service import Actor, AuthService
from sentinel.config import Settings
from sentinel.db.engine import session_scope
from sentinel.db.models import ApiKey
from sentinel.security.secret_files import read_secret, write_secret

log = structlog.get_logger("sentinel.ingestion")
KEY_NAME = "Alertmanager (system)"
_ACTOR = Actor.system("alertmanager-provisioning")


async def ensure_alertmanager_key(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> bool:
    """Returns True if a new key was written."""
    if not settings.alertmanager_key_file:
        return False
    path = Path(settings.alertmanager_key_file)
    async with session_scope(session_factory) as db:
        svc = AuthService(db, settings)
        current = read_secret(path)
        if current:
            principal = await svc.authenticate_key(current)
            if principal is not None and principal.key.name == KEY_NAME:
                return False
        for key_id in (
            await db.scalars(
                select(ApiKey.id).where(ApiKey.name == KEY_NAME, ApiKey.revoked_at.is_(None))
            )
        ).all():
            await svc.revoke_api_key(key_id, _ACTOR)
        _, plaintext = await svc.create_api_key(
            name=KEY_NAME, scopes=[KeyScope.ALERTS_WRITE], actor=_ACTOR, created_by=None
        )
        # 0644: Alertmanager runs as another user; only it and the API mount the volume.
        write_secret(path, plaintext, mode=0o644)
        log.info("alertmanager_key_provisioned")
        return True
