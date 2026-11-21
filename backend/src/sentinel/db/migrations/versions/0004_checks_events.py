"""Health checks, the event store, and the `prober` integration kind.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

check_kind = postgresql.ENUM("http", "tcp", "tls", name="check_kind", create_type=False)
probe_state = postgresql.ENUM("unknown", "up", "down", name="probe_state", create_type=False)
event_type = postgresql.ENUM(
    "deployment", "alert", "checkin", "cloud_change", "probe_state", "integration_status",
    name="event_type", create_type=False,
)  # fmt: skip


def _ts(name: str, *, nullable: bool = False, default: bool = True) -> sa.Column[object]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if default else None,
        nullable=nullable,
    )


def upgrade() -> None:
    # ADD VALUE can't run inside a transaction block on older Postgres; autocommit it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE integration_kind ADD VALUE IF NOT EXISTS 'prober'")
    for enum in (check_kind, probe_state, event_type):
        enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "health_checks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("integration_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", check_kind, nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("interval_s", sa.Integer(), nullable=False),
        sa.Column("timeout_s", sa.Float(), nullable=False),
        sa.Column(
            "assertions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=True),
        sa.Column("state", probe_state, server_default="unknown", nullable=False),
        _ts("state_changed_at", nullable=True, default=False),
        _ts("last_result_at", nullable=True, default=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="ck_health_checks_name_len"),
        sa.CheckConstraint(
            "char_length(target) BETWEEN 1 AND 2048", name="ck_health_checks_target_len"
        ),
        sa.CheckConstraint(
            "interval_s BETWEEN 5 AND 86400", name="ck_health_checks_interval_range"
        ),
        sa.CheckConstraint(
            "timeout_s > 0 AND timeout_s < interval_s",
            name="ck_health_checks_timeout_below_interval",
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"], ["integrations.id"],
            name="fk_health_checks_integration_id_integrations", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"], ["entities.id"],
            name="fk_health_checks_entity_id_entities", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"],
            name="fk_health_checks_created_by_users", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_health_checks"),
    )  # fmt: skip
    op.create_index(
        "ix_health_checks_integration_id", "health_checks", ["integration_id"], unique=False
    )

    op.create_table(
        "events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("type", event_type, nullable=False),
        sa.Column("source_integration_id", sa.UUID(), nullable=True),
        sa.Column(
            "entity_ids",
            postgresql.ARRAY(sa.UUID()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        _ts("occurred_at", default=False),
        _ts("received_at"),
        sa.Column("severity", sa.Text(), nullable=True),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "severity IS NULL OR severity IN ('info','warning','critical')",
            name="ck_events_severity_valid",
        ),
        sa.ForeignKeyConstraint(
            ["source_integration_id"], ["integrations.id"],
            name="fk_events_source_integration_id_integrations", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_events"),
        sa.UniqueConstraint("dedupe_key", name="uq_events_dedupe_key"),
    )  # fmt: skip
    op.create_index("ix_events_occurred_at", "events", ["occurred_at"], unique=False)
    op.create_index("ix_events_type_occurred_at", "events", ["type", "occurred_at"], unique=False)


def downgrade() -> None:
    op.drop_table("events")
    op.drop_table("health_checks")
    for enum in (event_type, probe_state, check_kind):
        enum.drop(op.get_bind(), checkfirst=True)
    # Postgres can't drop an enum value; `prober` stays in integration_kind.
    op.execute("DELETE FROM integrations WHERE kind = 'prober'")
