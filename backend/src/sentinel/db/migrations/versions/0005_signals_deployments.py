"""Signals, deployments, and the GitHub event types.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

signal_kind = postgresql.ENUM(
    "prom_alert", "error_group_new", "error_group_spike", "health", "telemetry_gap",
    name="signal_kind", create_type=False,
)  # fmt: skip
signal_status = postgresql.ENUM("firing", "resolved", name="signal_status", create_type=False)


def _ts(name: str, *, nullable: bool = False, default: bool = True) -> sa.Column[object]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if default else None,
        nullable=nullable,
    )


def _jsonb(name: str) -> sa.Column[object]:
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    )


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE event_type ADD VALUE IF NOT EXISTS 'push'")
        op.execute("ALTER TYPE event_type ADD VALUE IF NOT EXISTS 'workflow_run'")
    for enum in (signal_kind, signal_status):
        enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "signals",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("kind", signal_kind, nullable=False),
        sa.Column("status", signal_status, nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=True),
        sa.Column("service_name", sa.Text(), nullable=True),
        sa.Column("source_integration_id", sa.UUID(), nullable=True),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        _jsonb("labels"),
        _jsonb("payload"),
        _ts("starts_at", default=False),
        _ts("ends_at", nullable=True, default=False),
        _ts("received_at"),
        _ts("updated_at"),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "severity IN ('critical','warning','info')", name="ck_signals_severity_valid"
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"], ["entities.id"], name="fk_signals_entity_id_entities", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["source_integration_id"], ["integrations.id"],
            name="fk_signals_source_integration_id_integrations", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_signals"),
        sa.UniqueConstraint("dedupe_key", name="uq_signals_dedupe_key"),
    )  # fmt: skip
    op.create_index("ix_signals_status_starts_at", "signals", ["status", "starts_at"])
    op.create_index("ix_signals_service_name_starts_at", "signals", ["service_name", "starts_at"])
    op.create_index("ix_signals_entity_id_starts_at", "signals", ["entity_id", "starts_at"])

    op.create_table(
        "deployments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=True),
        sa.Column("service_name", sa.Text(), nullable=False),
        sa.Column("environment", sa.Text(), server_default="dev", nullable=False),
        sa.Column("version", sa.Text(), nullable=True),
        sa.Column("image_ref", sa.Text(), nullable=True),
        sa.Column("commit_sha", sa.Text(), nullable=True),
        sa.Column("previous_commit_sha", sa.Text(), nullable=True),
        _ts("deployed_at", default=False),
        sa.Column("deployed_by", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "source IN ('ci','script','rollback','github')", name="ck_deployments_source_valid"
        ),
        sa.CheckConstraint(
            "status IN ('succeeded','failed','in_progress')", name="ck_deployments_status_valid"
        ),
        sa.CheckConstraint(
            "commit_sha IS NULL OR commit_sha ~ '^[0-9a-f]{7,64}$'",
            name="ck_deployments_commit_sha_hex",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], name="fk_deployments_event_id_events", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"], ["entities.id"],
            name="fk_deployments_entity_id_entities", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deployments"),
        sa.UniqueConstraint("event_id", name="uq_deployments_event_id"),
    )  # fmt: skip
    op.create_index(
        "ix_deployments_service_name_deployed_at", "deployments", ["service_name", "deployed_at"]
    )


def downgrade() -> None:
    op.drop_table("deployments")
    op.drop_table("signals")
    for enum in (signal_status, signal_kind):
        enum.drop(op.get_bind(), checkfirst=True)
    # Postgres can't drop enum values; `push` and `workflow_run` stay in event_type.
    op.execute("DELETE FROM events WHERE type IN ('push', 'workflow_run')")
