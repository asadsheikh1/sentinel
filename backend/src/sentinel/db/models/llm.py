"""LLM call telemetry: one row per request to the model runtime (Step 15).

``llm_calls`` holds what is needed to measure cost and reliability (tokens,
latency, time to first token, success). The prompt itself is stored only as a
SHA-256. Prompts and outputs go to ``llm_call_payloads`` only when
``SENTINEL_LLM_STORE_PAYLOADS`` is on, and are pruned after
``SENTINEL_LLM_PAYLOAD_RETENTION_DAYS``.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from sentinel.db.base import Base, UUIDPrimaryKey

# Why a call failed. `schema` = the reply did not validate against the schema.
LLM_ERROR_KINDS = ("unavailable", "timeout", "http", "schema", "empty", "cancelled")


class LLMCall(UUIDPrimaryKey, Base):
    __tablename__ = "llm_calls"
    __table_args__ = (
        CheckConstraint("operation IN ('chat','embed')", name="operation_valid"),
        CheckConstraint(
            "error_kind IS NULL OR error_kind IN ("
            + ", ".join(f"'{k}'" for k in LLM_ERROR_KINDS)
            + ")",
            name="error_kind_valid",
        ),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        Index("ix_llm_calls_created_at", "created_at"),
        Index("ix_llm_calls_task_created_at", "task", "created_at"),
        Index(
            "ix_llm_calls_investigation_id",
            "investigation_id",
            postgresql_where="investigation_id IS NOT NULL",
        ),
    )

    # The `investigations` table arrives in Step 17; the foreign key with it.
    investigation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # One generate_structured() call = one request_id; its attempts share it.
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    operation: Mapped[str] = mapped_column(Text, nullable=False, default="chat")
    task: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False, default="")
    prompt_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    thinking: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    num_ctx: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    ttft_ms: Mapped[int | None] = mapped_column(Integer)
    load_ms: Mapped[int | None] = mapped_column(Integer)  # model load time, if it loaded
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_kind: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LLMCallPayload(Base):
    """The request messages and the raw reply (opt-in, pruned)."""

    __tablename__ = "llm_call_payloads"
    __table_args__ = (Index("ix_llm_call_payloads_created_at", "created_at"),)

    llm_call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("llm_calls.id", ondelete="CASCADE"), primary_key=True
    )
    request: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    response: Mapped[str | None] = mapped_column(Text)
    thinking: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
