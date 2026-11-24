"""Incidents: correlated signals, their entities, and an append-only timeline."""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sentinel.db.base import Base, UUIDPrimaryKey, enum_values


class IncidentStatus(enum.StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"
    CLOSED = "closed"
    FALSE_POSITIVE = "false_positive"


class Severity(enum.StrEnum):
    """SEV1 is the most severe."""

    SEV1 = "SEV1"
    SEV2 = "SEV2"
    SEV3 = "SEV3"
    SEV4 = "SEV4"


# Why a signal belongs to its incident.
ATTACH_REASONS = (
    "first_signal",
    "same_entity",
    "same_service",
    "related_entity",
    "shared_trace",
    "deploy_window",
)
EVENT_TYPES = (
    "opened",
    "signal_attached",
    "severity_changed",
    "status_changed",
    "deployment",
    "investigation_queued",
    "investigation_started",
    "hypothesis_added",
    "action_proposed",
    "action_approved",
    "action_executed",
    "comment",
    "feedback",
)


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


class Incident(UUIDPrimaryKey, Base):
    __tablename__ = "incidents"

    number: Mapped[int] = mapped_column(Integer, Identity(always=True), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[IncidentStatus] = mapped_column(
        Enum(IncidentStatus, name="incident_status", values_callable=enum_values), nullable=False
    )
    severity: Mapped[Severity] = mapped_column(
        Enum(Severity, name="incident_severity", values_callable=enum_values), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_signal_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    mitigated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    primary_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("entities.id", ondelete="SET NULL")
    )
    service_name: Mapped[str | None] = mapped_column(Text)
    suspected_deployment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("deployments.id", ondelete="SET NULL")
    )
    summary: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text, nullable=False, server_default="system")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index(None, "status", "last_signal_at"),
        Index(None, "service_name", "started_at"),
        Index(None, "started_at"),
    )

    @property
    def ref(self) -> str:
        return f"INC-{self.number}"


class IncidentEntity(Base):
    """An entity an incident involves: where it started (origin) or what it reached."""

    __tablename__ = "incident_entities"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), primary_key=True
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("role IN ('origin','affected')", name="role_valid"),
        Index(None, "entity_id"),
    )


class IncidentSignal(Base):
    """A signal belongs to at most one incident."""

    __tablename__ = "incident_signals"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), primary_key=True
    )
    signal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("signals.id", ondelete="CASCADE"), primary_key=True, unique=True
    )
    attached_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (CheckConstraint(f"reason IN {_in(ATTACH_REASONS)}", name="reason_valid"),)


class IncidentEvent(Base):
    """The timeline. Append-only: a trigger rejects UPDATE and DELETE."""

    __tablename__ = "incident_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)  # "system", "user:<id>", ...
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        CheckConstraint(f"type IN {_in(EVENT_TYPES)}", name="type_valid"),
        Index(None, "incident_id", "ts"),
    )
