"""Integrations, ingest tokens, signal status, entities and relations.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

integration_kind = postgresql.ENUM(
    "linux_host", "container_host", "otel_app", "http_check", "deploy_webhook", "github",
    "alertmanager", "postgresql", "checkin", "aws_account", "upload",
    name="integration_kind", create_type=False,
)  # fmt: skip
integration_status = postgresql.ENUM(
    "pending", "healthy", "degraded", "stale", "disabled", "revoked",
    name="integration_status", create_type=False,
)  # fmt: skip
entity_type = postgresql.ENUM(
    "host", "container", "service", "database", "endpoint", "job", "cloud_resource", "repository",
    name="entity_type", create_type=False,
)  # fmt: skip


def _ts(name: str, *, nullable: bool = False, default: bool = True) -> sa.Column[object]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if default else None,
        nullable=nullable,
    )


def upgrade() -> None:
    for enum in (integration_kind, integration_status, entity_type):
        enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "integrations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", integration_kind, nullable=False),
        sa.Column("environment", sa.Text(), server_default="dev", nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "expected_signals",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("status", integration_status, server_default="pending", nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 100", name=op.f("ck_integrations_name_len")
        ),
        sa.CheckConstraint(
            "expected_signals <@ ARRAY['metrics','logs','traces','events']::text[]",
            name=op.f("ck_integrations_expected_signals_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_integrations_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integrations")),
        sa.UniqueConstraint("name", name=op.f("uq_integrations_name")),
    )

    op.create_table(
        "ingest_tokens",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("integration_id", sa.UUID(), nullable=False),
        sa.Column("prefix", sa.String(length=8), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("rate_limit_per_min", sa.Integer(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        _ts("created_at"),
        sa.CheckConstraint(
            "char_length(token_hash) = 64", name=op.f("ck_ingest_tokens_token_hash_sha256_hex")
        ),
        sa.CheckConstraint("char_length(prefix) = 8", name=op.f("ck_ingest_tokens_prefix_len")),
        sa.CheckConstraint(
            "rate_limit_per_min >= 1", name=op.f("ck_ingest_tokens_rate_limit_positive")
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"],
            ["integrations.id"],
            name=op.f("fk_ingest_tokens_integration_id_integrations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingest_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_ingest_tokens_token_hash")),
    )
    op.create_index(op.f("ix_ingest_tokens_integration_id"), "ingest_tokens", ["integration_id"])

    op.create_table(
        "integration_signal_status",
        sa.Column("integration_id", sa.UUID(), nullable=False),
        sa.Column("signal", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_count_window", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "signal IN ('metrics','logs','traces','events')",
            name=op.f("ck_integration_signal_status_signal_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"],
            ["integrations.id"],
            name=op.f("fk_integration_signal_status_integration_id_integrations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "integration_id", "signal", name=op.f("pk_integration_signal_status")
        ),
    )

    op.create_table(
        "entities",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("type", entity_type, nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "integration_ids",
            postgresql.ARRAY(sa.UUID()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("criticality", sa.Text(), nullable=True),
        _ts("first_seen"),
        _ts("last_seen"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entities")),
        sa.UniqueConstraint("type", "key", name=op.f("uq_entities_type_key")),
    )
    op.create_index(op.f("ix_entities_last_seen"), "entities", ["last_seen"])

    op.create_table(
        "entity_relations",
        sa.Column("from_entity_id", sa.UUID(), nullable=False),
        sa.Column("to_entity_id", sa.UUID(), nullable=False),
        sa.Column("relation", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), server_default="inferred", nullable=False),
        sa.Column("confidence", sa.Float(), server_default=sa.text("1.0"), nullable=False),
        _ts("last_seen"),
        sa.CheckConstraint(
            "relation IN ('runs_on','uses','fronts','built_from','member_of')",
            name=op.f("ck_entity_relations_relation_valid"),
        ),
        sa.CheckConstraint(
            "source IN ('inferred','manual')", name=op.f("ck_entity_relations_source_valid")
        ),
        sa.CheckConstraint(
            "confidence BETWEEN 0 AND 1", name=op.f("ck_entity_relations_confidence_range")
        ),
        sa.ForeignKeyConstraint(
            ["from_entity_id"],
            ["entities.id"],
            name=op.f("fk_entity_relations_from_entity_id_entities"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["to_entity_id"],
            ["entities.id"],
            name=op.f("fk_entity_relations_to_entity_id_entities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "from_entity_id", "to_entity_id", "relation", name=op.f("pk_entity_relations")
        ),
    )
    op.create_index(op.f("ix_entity_relations_to_entity_id"), "entity_relations", ["to_entity_id"])

    # sentinel_readonly must not read token hashes.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sentinel_readonly') THEN
            REVOKE ALL ON ingest_tokens FROM sentinel_readonly;
          END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_entity_relations_to_entity_id"), table_name="entity_relations")
    op.drop_table("entity_relations")
    op.drop_index(op.f("ix_entities_last_seen"), table_name="entities")
    op.drop_table("entities")
    op.drop_table("integration_signal_status")
    op.drop_index(op.f("ix_ingest_tokens_integration_id"), table_name="ingest_tokens")
    op.drop_table("ingest_tokens")
    op.drop_table("integrations")
    for enum in (entity_type, integration_status, integration_kind):
        enum.drop(op.get_bind(), checkfirst=True)
