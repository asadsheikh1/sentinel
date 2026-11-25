"""Integrations (monitored sources), their ingest tokens, health, and discovered entities."""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from sentinel.db.base import Base, CreatedAt, Timestamps, UUIDPrimaryKey, enum_values


class IntegrationKind(enum.StrEnum):
    LINUX_HOST = "linux_host"
    CONTAINER_HOST = "container_host"
    OTEL_APP = "otel_app"
    HTTP_CHECK = "http_check"
    DEPLOY_WEBHOOK = "deploy_webhook"
    GITHUB = "github"
    ALERTMANAGER = "alertmanager"
    POSTGRESQL = "postgresql"
    CHECKIN = "checkin"
    AWS_ACCOUNT = "aws_account"
    UPLOAD = "upload"
    PROBER = "prober"  # SENTINEL's own prober (one, provisioned by the API)


class IntegrationStatus(enum.StrEnum):
    PENDING = "pending"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STALE = "stale"
    DISABLED = "disabled"
    REVOKED = "revoked"


class EntityType(enum.StrEnum):
    HOST = "host"
    CONTAINER = "container"
    SERVICE = "service"
    DATABASE = "database"
    ENDPOINT = "endpoint"
    JOB = "job"
    CLOUD_RESOURCE = "cloud_resource"
    REPOSITORY = "repository"


SIGNALS = ("metrics", "logs", "traces", "events")


class Integration(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "integrations"

    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    kind: Mapped[IntegrationKind] = mapped_column(
        Enum(IntegrationKind, name="integration_kind", values_callable=enum_values), nullable=False
    )
    environment: Mapped[str] = mapped_column(Text, nullable=False, server_default="dev")
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_signals: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    status: Mapped[IntegrationStatus] = mapped_column(
        Enum(IntegrationStatus, name="integration_status", values_callable=enum_values),
        nullable=False,
        server_default=IntegrationStatus.PENDING.value,
    )
    status_reason: Mapped[str | None] = mapped_column(Text)
    # When the installer's test marker was seen in the logs (the log path works end to end).
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="name_len"),
        CheckConstraint(
            "expected_signals <@ ARRAY['metrics','logs','traces','events']::text[]",
            name="expected_signals_valid",
        ),
    )


class IngestToken(UUIDPrimaryKey, CreatedAt, Base):
    """Write-only credential for pushing telemetry. Only a keyed hash is stored."""

    __tablename__ = "ingest_tokens"

    integration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integrations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    prefix: Mapped[str] = mapped_column(String(8), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    rate_limit_per_min: Mapped[int] = mapped_column(Integer, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("char_length(token_hash) = 64", name="token_hash_sha256_hex"),
        CheckConstraint("char_length(prefix) = 8", name="prefix_len"),
        CheckConstraint("rate_limit_per_min >= 1", name="rate_limit_positive"),
    )


class IntegrationSignalStatus(Base):
    __tablename__ = "integration_signal_status"

    integration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integrations.id", ondelete="CASCADE"), primary_key=True
    )
    signal: Mapped[str] = mapped_column(Text, primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_count_window: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("signal IN ('metrics','logs','traces','events')", name="signal_valid"),
    )


class Entity(UUIDPrimaryKey, Base):
    """Something being monitored, discovered from telemetry resource attributes."""

    __tablename__ = "entities"

    type: Mapped[EntityType] = mapped_column(
        Enum(EntityType, name="entity_type", values_callable=enum_values), nullable=False
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    integration_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    criticality: Mapped[str | None] = mapped_column(Text)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("type", "key"), Index(None, "last_seen"))


class EntityRelation(Base):
    __tablename__ = "entity_relations"

    from_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
    )
    to_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    relation: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="inferred")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("1.0"))
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "relation IN ('runs_on','uses','fronts','built_from','member_of')",
            name="relation_valid",
        ),
        CheckConstraint("source IN ('inferred','manual','traces')", name="source_valid"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence_range"),
    )


__all__ = [
    "SIGNALS",
    "Entity",
    "EntityRelation",
    "EntityType",
    "IngestToken",
    "Integration",
    "IntegrationKind",
    "IntegrationSignalStatus",
    "IntegrationStatus",
]
