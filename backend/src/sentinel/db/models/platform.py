"""Platform tables: audit log and job queue (schema only in Step 2)."""

import enum
from datetime import datetime
from ipaddress import IPv4Address, IPv6Address
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sentinel.db.base import Base, Timestamps, UUIDPrimaryKey, enum_values


class ActorType(enum.StrEnum):
    USER = "user"
    API_KEY = "api_key"
    SYSTEM = "system"
    LLM = "llm"


class AuditLog(Base):
    """Append-only, hash-chained audit trail.

    ``hash = SHA256(prev_hash || canonical_json(row))``. The chain writer and
    ``GET /audit/verify`` arrive in Step 3. Immutability is enforced twice:
    the ``sentinel_app`` role has no UPDATE/DELETE grant, and a trigger rejects
    UPDATE/DELETE/TRUNCATE for every role (defence in depth).
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actor_type: Mapped[ActorType] = mapped_column(
        Enum(ActorType, name="actor_type", values_callable=enum_values),
        nullable=False,
    )
    actor_id: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    ip: Mapped[IPv4Address | IPv6Address | None] = mapped_column(INET)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    __table_args__ = (
        CheckConstraint("char_length(prev_hash) = 64", name="prev_hash_len"),
        CheckConstraint("char_length(hash) = 64", name="hash_len"),
        Index(None, "ts"),
        Index(None, "target_type", "target_id"),
    )


JOB_STATUSES = ("queued", "running", "succeeded", "failed", "dead")


class Job(UUIDPrimaryKey, Timestamps, Base):
    """Postgres-backed job queue (ADR-0006). Worker logic arrives in Step 17."""

    __tablename__ = "jobs"

    queue: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(Text, unique=True)

    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in JOB_STATUSES) + ")", name="status_valid"
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        Index(None, "queue", "status", "run_after", "priority"),
        # Hot path for claiming work: only queued rows are indexed.
        Index(
            "ix_jobs_claimable",
            "queue",
            text("priority DESC"),
            "run_after",
            postgresql_where=text("status = 'queued'"),
        ),
    )
