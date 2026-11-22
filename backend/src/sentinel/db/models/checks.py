"""External health checks and the canonical event store (``SentinelEvent``)."""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from sentinel.db.base import Base, Timestamps, UUIDPrimaryKey, enum_values


class CheckKind(enum.StrEnum):
    HTTP = "http"
    TCP = "tcp"
    TLS = "tls"


class ProbeState(enum.StrEnum):
    UNKNOWN = "unknown"  # no result yet (or none recently)
    UP = "up"
    DOWN = "down"  # failed twice in a row


class EventType(enum.StrEnum):
    DEPLOYMENT = "deployment"
    ALERT = "alert"
    CHECKIN = "checkin"
    CLOUD_CHANGE = "cloud_change"
    PROBE_STATE = "probe_state"
    INTEGRATION_STATUS = "integration_status"


class HealthCheck(UUIDPrimaryKey, Timestamps, Base):
    """An HTTP(S), TCP or TLS check run from outside by the prober."""

    __tablename__ = "health_checks"

    integration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integrations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[CheckKind] = mapped_column(
        Enum(CheckKind, name="check_kind", values_callable=enum_values), nullable=False
    )
    target: Mapped[str] = mapped_column(Text, nullable=False)
    interval_s: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_s: Mapped[float] = mapped_column(Float, nullable=False)
    assertions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("entities.id", ondelete="SET NULL")
    )
    state: Mapped[ProbeState] = mapped_column(
        Enum(ProbeState, name="probe_state", values_callable=enum_values),
        nullable=False,
        server_default=ProbeState.UNKNOWN.value,
    )
    state_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_result_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="name_len"),
        CheckConstraint("char_length(target) BETWEEN 1 AND 2048", name="target_len"),
        # The API enforces the configured minimum (30 s in prod); this is the floor.
        CheckConstraint("interval_s BETWEEN 5 AND 86400", name="interval_range"),
        CheckConstraint("timeout_s > 0 AND timeout_s < interval_s", name="timeout_below_interval"),
    )


class Event(UUIDPrimaryKey, Base):
    """``SentinelEvent``: deployments, alerts, check-ins, cloud and state changes."""

    __tablename__ = "events"

    type: Mapped[EventType] = mapped_column(
        Enum(EventType, name="event_type", values_callable=enum_values), nullable=False
    )
    source_integration_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("integrations.id", ondelete="SET NULL")
    )
    entity_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    severity: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        CheckConstraint(
            "severity IS NULL OR severity IN ('info','warning','critical')", name="severity_valid"
        ),
        Index(None, "occurred_at"),
        Index(None, "type", "occurred_at"),
    )
