"""Error groups (log fingerprints) and poll watermarks.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts(name: str, *, default: bool = True) -> sa.Column[object]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if default else None,
        nullable=False,
    )


def _jsonb(name: str) -> sa.Column[object]:
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "error_groups",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=True),
        sa.Column("service_name", sa.Text(), nullable=True),
        sa.Column("source_integration_id", sa.UUID(), nullable=True),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("exception_type", sa.Text(), server_default="", nullable=False),
        sa.Column("normalized_message", sa.Text(), nullable=False),
        sa.Column("top_frame", sa.Text(), server_default="", nullable=False),
        sa.Column("level", sa.Text(), server_default="error", nullable=False),
        _ts("first_seen", default=False),
        _ts("last_seen", default=False),
        sa.Column("total_count", sa.BigInteger(), server_default="0", nullable=False),
        _jsonb("minute_counts"),
        sa.Column(
            "sample_trace_ids", postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"), nullable=False,
        ),
        _jsonb("sample_log"),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint("level IN ('error','critical')", name="ck_error_groups_level_valid"),
        sa.ForeignKeyConstraint(
            ["entity_id"], ["entities.id"],
            name="fk_error_groups_entity_id_entities", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_integration_id"], ["integrations.id"],
            name="fk_error_groups_source_integration_id_integrations", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_error_groups"),
        sa.UniqueConstraint(
            "scope_key", "fingerprint", name="uq_error_groups_scope_key_fingerprint"
        ),
    )  # fmt: skip
    op.create_index(
        "ix_error_groups_service_name_last_seen", "error_groups", ["service_name", "last_seen"]
    )
    op.create_index(
        "ix_error_groups_entity_id_last_seen", "error_groups", ["entity_id", "last_seen"]
    )
    op.create_index("ix_error_groups_last_seen", "error_groups", ["last_seen"])

    op.create_table(
        "poll_watermarks",
        sa.Column("source", sa.Text(), nullable=False),
        _ts("last_ts", default=False),
        sa.Column("last_ns", sa.BigInteger(), nullable=False),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("source", name="pk_poll_watermarks"),
    )


def downgrade() -> None:
    op.drop_table("poll_watermarks")
    op.drop_table("error_groups")
