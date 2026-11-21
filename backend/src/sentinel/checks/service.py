"""Health checks: validation (SSRF guard at creation), CRUD, audit, endpoint entities.

The prober validates every target again before each connection; this first
check gives the user an immediate, clear error and keeps unsafe targets out of
the database.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.auth.service import Actor, ConflictError
from sentinel.config import Settings
from sentinel.db.models import (
    CheckKind,
    Entity,
    EntityType,
    HealthCheck,
    Integration,
    IntegrationKind,
    IntegrationStatus,
)
from sentinel.prober.ssrf_guard import (
    BlockedTargetError,
    Guard,
    UnresolvableTargetError,
    parse_url,
    split_host_port,
)
from sentinel.security import audit
from sentinel.security.redaction import redact_text

JsonScalar = str | int | float | bool | None
_JSON_PATH = r"^[A-Za-z0-9_-]{1,64}(\.[A-Za-z0-9_-]{1,64}){0,9}$"  # e.g. checks.0.status


class JsonField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Annotated[
        str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}(\.[A-Za-z0-9_-]{1,64}){0,9}$")
    ]
    equals: JsonScalar = None


class HttpAssertions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_status: list[Annotated[int, Field(ge=100, le=599)]] = Field(
        default_factory=list, max_length=20
    )
    body_contains: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    json_field: JsonField | None = None


class TlsAssertions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_days_valid: int | None = Field(default=None, ge=1, le=365)


class TcpAssertions(BaseModel):
    model_config = ConfigDict(extra="forbid")


_ASSERTIONS: dict[CheckKind, type[BaseModel]] = {
    CheckKind.HTTP: HttpAssertions,
    CheckKind.TLS: TlsAssertions,
    CheckKind.TCP: TcpAssertions,
}


class CheckInput(BaseModel):
    """What a user configures. Bounds that depend on settings are checked in the service."""

    model_config = ConfigDict(extra="forbid")

    integration_id: uuid.UUID
    name: Annotated[str, StringConstraints(min_length=1, max_length=100, strip_whitespace=True)]
    kind: CheckKind
    target: Annotated[str, StringConstraints(min_length=1, max_length=2048, strip_whitespace=True)]
    interval_s: int = Field(default=60, le=86400)
    timeout_s: float = Field(default=10.0, ge=0.5, le=30)
    assertions: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @model_validator(mode="after")
    def _shape(self) -> "CheckInput":
        model = _ASSERTIONS[self.kind]
        try:
            parsed = model.model_validate(self.assertions)
        except ValidationError as e:
            first = e.errors()[0]
            where = ".".join(str(p) for p in first["loc"])
            raise ValueError(f"assertions.{where}: {first['msg']} (for a {self.kind} check)") from e
        self.assertions = parsed.model_dump(exclude_none=True)
        if self.timeout_s >= self.interval_s:
            raise ValueError("timeout_s must be shorter than interval_s")
        return self


class CheckError(ValueError):
    """Invalid check (shown to the user as 422)."""


def normalise_target(kind: CheckKind, target: str) -> tuple[str, str, int]:
    """(canonical target, host, port). Raises BlockedTargetError on bad syntax."""
    if kind is CheckKind.HTTP:
        parts = parse_url(target)
        host = (parts.hostname or "").lower()
        port = parts.port or (443 if parts.scheme == "https" else 80)
        netloc = parts.netloc.lower()
        return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, "")), host, port
    host, port = split_host_port(target, default_port=443 if kind is CheckKind.TLS else None)
    host = host.lower().strip("[]")
    shown = f"[{host}]" if ":" in host else host
    return f"{shown}:{port}", host, port


def entity_key(kind: CheckKind, target: str) -> str:
    if kind is CheckKind.HTTP:
        parts = urlsplit(target)
        return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))
    return f"{kind.value}://{target}"


class CheckService:
    def __init__(self, db: AsyncSession, settings: Settings, guard: Guard | None = None) -> None:
        self.db = db
        self.settings = settings
        self.guard = guard or Guard(allow_hosts=frozenset(settings.prober_allow_hosts))

    async def _audit(
        self, actor: Actor, action: str, check: HealthCheck, details: dict[str, Any]
    ) -> None:
        await audit.append(
            self.db,
            actor_type=actor.type,
            actor_id=actor.id,
            action=action,
            target_type="health_check",
            target_id=str(check.id),
            details=details,
            ip=actor.ip,
        )

    async def list_all(self) -> Sequence[HealthCheck]:
        return (await self.db.scalars(select(HealthCheck).order_by(HealthCheck.name))).all()

    async def get(self, check_id: uuid.UUID) -> HealthCheck | None:
        return await self.db.get(HealthCheck, check_id)

    async def _validate(self, data: CheckInput) -> str:
        if data.interval_s < self.settings.health_check_min_interval_s:
            raise CheckError(
                f"interval_s must be at least {self.settings.health_check_min_interval_s} seconds"
            )
        integration = await self.db.get(Integration, data.integration_id)
        if integration is None or integration.kind is not IntegrationKind.HTTP_CHECK:
            raise CheckError("integration_id must be an http_check integration")
        if integration.status is IntegrationStatus.DISABLED:
            raise CheckError("That integration is disabled")
        try:
            target, host, port = normalise_target(data.kind, data.target)
            # Allowlisted dev hosts (the demo shop) live on a network the API isn't
            # on, so they can't be resolved here; the prober resolves them.
            if host not in self.guard.allow_hosts:
                await self.guard.vet(host, port)  # resolves now; the prober re-checks every run
        except UnresolvableTargetError as e:
            raise CheckError(f"Target does not resolve: {e}") from e
        except BlockedTargetError as e:
            raise CheckError(f"Target not allowed: {e}") from e
        return target

    async def _endpoint_entity(self, check: HealthCheck) -> uuid.UUID:
        key = entity_key(check.kind, check.target)
        entity = await self.db.scalar(
            select(Entity).where(Entity.type == EntityType.ENDPOINT, Entity.key == key)
        )
        now = datetime.now(UTC)
        attrs = {"check.kind": check.kind.value, "check.target": redact_text(check.target).text}
        if entity is None:
            entity = Entity(
                type=EntityType.ENDPOINT,
                key=key,
                display_name=check.name,
                attributes=attrs,
                integration_ids=[check.integration_id],
                first_seen=now,
                last_seen=now,
            )
            self.db.add(entity)
            await self.db.flush()
        else:
            entity.attributes = {**entity.attributes, **attrs}
            entity.integration_ids = sorted(set(entity.integration_ids) | {check.integration_id})
        return entity.id

    async def create(
        self, data: CheckInput, *, actor: Actor, created_by: uuid.UUID | None
    ) -> HealthCheck:
        count = await self.db.scalar(select(func.count()).select_from(HealthCheck))
        if (count or 0) >= self.settings.health_check_max:
            raise ConflictError(
                f"At most {self.settings.health_check_max} health checks can be configured."
            )
        target = await self._validate(data)
        check = HealthCheck(
            integration_id=data.integration_id,
            name=data.name,
            kind=data.kind,
            target=target,
            interval_s=data.interval_s,
            timeout_s=data.timeout_s,
            assertions=data.assertions,
            enabled=data.enabled,
            created_by=created_by,
        )
        self.db.add(check)
        await self.db.flush()
        check.entity_id = await self._endpoint_entity(check)
        await self._audit(
            actor,
            "check.create",
            check,
            {"name": check.name, "kind": check.kind.value, "target": redact_text(target).text},
        )
        return check

    async def update(self, check: HealthCheck, data: CheckInput, *, actor: Actor) -> HealthCheck:
        target = await self._validate(data)
        before = (check.target, check.kind, check.interval_s, check.enabled)
        check.integration_id = data.integration_id
        check.name = data.name
        check.kind = data.kind
        check.target = target
        check.interval_s = data.interval_s
        check.timeout_s = data.timeout_s
        check.assertions = data.assertions
        check.enabled = data.enabled
        await self.db.flush()
        check.entity_id = await self._endpoint_entity(check)
        await self._audit(
            actor,
            "check.update",
            check,
            {
                "target_changed": before[0] != target or before[1] != data.kind,
                "interval_s": data.interval_s,
                "enabled": data.enabled,
            },
        )
        return check

    async def delete(self, check: HealthCheck, *, actor: Actor) -> None:
        await self._audit(actor, "check.delete", check, {"name": check.name})
        await self.db.delete(check)
        await self.db.flush()

    async def for_prober(self) -> list[HealthCheck]:
        """Enabled checks of integrations that are not disabled."""
        stmt = (
            select(HealthCheck)
            .join(Integration, HealthCheck.integration_id == Integration.id)
            .where(
                HealthCheck.enabled.is_(True),
                Integration.status.not_in([IntegrationStatus.DISABLED]),
            )
            .order_by(HealthCheck.id)
        )
        return list((await self.db.scalars(stmt)).all())
