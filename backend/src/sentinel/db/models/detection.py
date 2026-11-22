"""Detection: signals (alerts, telemetry gaps, error groups) and deployments."""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sentinel.db.base import Base, UUIDPrimaryKey, enum_values


class SignalKind(enum.StrEnum):
    PROM_ALERT = "prom_alert"  # a rule-pack alert, via Alertmanager
    ERROR_GROUP_NEW = "error_group_new"  # Step 11
    ERROR_GROUP_SPIKE = "error_group_spike"  # Step 11
    HEALTH = "health"
    TELEMETRY_GAP = "telemetry_gap"  # a source went quiet


class SignalStatus(enum.StrEnum):
    FIRING = "firing"
    RESOLVED = "resolved"


class Signal(UUIDPrimaryKey, Base):
    """Something that might be part of an incident. Grouping into incidents is Step 12."""

    __tablename__ = "signals"

    kind: Mapped[SignalKind] = mapped_column(
        Enum(SignalKind, name="signal_kind", values_callable=enum_values), nullable=False
    )
    status: Mapped[SignalStatus] = mapped_column(
        Enum(SignalStatus, name="signal_status", values_callable=enum_values), nullable=False
    )
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("entities.id", ondelete="SET NULL")
    )
    service_name: Mapped[str | None] = mapped_column(Text)
    source_integration_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("integrations.id", ondelete="SET NULL")
    )
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    labels: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    __table_args__ = (
        CheckConstraint("severity IN ('critical','warning','info')", name="severity_valid"),
        Index(None, "status", "starts_at"),
        Index(None, "service_name", "starts_at"),
        Index(None, "entity_id", "starts_at"),
    )


class Deployment(UUIDPrimaryKey, Base):
    """A deployment, fed from ``deployment`` events (typed, for fast lookups)."""

    __tablename__ = "deployments"

    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("entities.id", ondelete="SET NULL")
    )
    service_name: Mapped[str] = mapped_column(Text, nullable=False)
    environment: Mapped[str] = mapped_column(Text, nullable=False, server_default="dev")
    version: Mapped[str | None] = mapped_column(Text)
    image_ref: Mapped[str | None] = mapped_column(Text)
    commit_sha: Mapped[str | None] = mapped_column(Text)
    previous_commit_sha: Mapped[str | None] = mapped_column(Text)
    deployed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deployed_by: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("source IN ('ci','script','rollback','github')", name="source_valid"),
        CheckConstraint("status IN ('succeeded','failed','in_progress')", name="status_valid"),
        CheckConstraint(
            "commit_sha IS NULL OR commit_sha ~ '^[0-9a-f]{7,64}$'", name="commit_sha_hex"
        ),
        Index(None, "service_name", "deployed_at"),
    )
