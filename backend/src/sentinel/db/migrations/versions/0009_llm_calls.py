"""LLM call telemetry: llm_calls and (opt-in) llm_call_payloads.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IF_ROLE = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sentinel_readonly') THEN
    {stmt}
  END IF;
END
$$;
"""


def upgrade() -> None:
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("investigation_id", sa.UUID(), nullable=True),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("attempt", sa.SmallInteger(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("prompt_sha256", sa.Text(), nullable=False),
        sa.Column("thinking", sa.Boolean(), nullable=False),
        sa.Column("num_ctx", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("ttft_ms", sa.Integer(), nullable=True),
        sa.Column("load_ms", sa.Integer(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_kind", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("operation IN ('chat','embed')", name=op.f("ck_llm_calls_operation_valid")),
        sa.CheckConstraint(
            "error_kind IS NULL OR error_kind IN ('unavailable', 'timeout', 'http', 'schema', 'empty', 'cancelled')",
            name=op.f("ck_llm_calls_error_kind_valid"),
        ),
        sa.CheckConstraint("attempt >= 1", name=op.f("ck_llm_calls_attempt_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_calls")),
    )  # fmt: skip
    op.create_index("ix_llm_calls_created_at", "llm_calls", ["created_at"])
    op.create_index("ix_llm_calls_task_created_at", "llm_calls", ["task", "created_at"])
    op.create_index(
        "ix_llm_calls_investigation_id",
        "llm_calls",
        ["investigation_id"],
        postgresql_where=sa.text("investigation_id IS NOT NULL"),
    )

    op.create_table(
        "llm_call_payloads",
        sa.Column("llm_call_id", sa.UUID(), nullable=False),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("response", sa.Text(), nullable=True),
        sa.Column("thinking", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["llm_call_id"], ["llm_calls.id"],
            name=op.f("fk_llm_call_payloads_llm_call_id_llm_calls"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("llm_call_id", name=op.f("pk_llm_call_payloads")),
    )  # fmt: skip
    op.create_index("ix_llm_call_payloads_created_at", "llm_call_payloads", ["created_at"])
    # Prompts carry (redacted) evidence from logs and code: not for read-only users.
    op.execute(_IF_ROLE.format(stmt="REVOKE ALL ON llm_call_payloads FROM sentinel_readonly;"))


def downgrade() -> None:
    op.drop_table("llm_call_payloads")
    op.drop_table("llm_calls")
