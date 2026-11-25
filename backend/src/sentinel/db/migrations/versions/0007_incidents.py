"""Incidents, their entities and signals, and the append-only timeline.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies (a migration must not change when the models do).
ATTACH_REASONS = (
    "first_signal", "same_entity", "same_service", "related_entity", "shared_trace",
    "deploy_window",
)  # fmt: skip
EVENT_TYPES = (
    "opened", "signal_attached", "severity_changed", "status_changed", "deployment",
    "investigation_queued", "investigation_started", "hypothesis_added", "action_proposed",
    "action_approved", "action_executed", "comment", "feedback",
)  # fmt: skip

incident_status = postgresql.ENUM(
    "open", "investigating", "mitigated", "resolved", "closed", "false_positive",
    name="incident_status", create_type=False,
)  # fmt: skip
incident_severity = postgresql.ENUM(
    "SEV1", "SEV2", "SEV3", "SEV4", name="incident_severity", create_type=False
)


_IF_ROLE = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sentinel_app') THEN
    {stmt}
  END IF;
END
$$;
"""


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _ts(name: str, *, nullable: bool = False, default: bool = True) -> sa.Column[object]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if default else None,
        nullable=nullable,
    )


def upgrade() -> None:
    # Relations learnt from the trace store's call graph ("uses") say so.
    op.drop_constraint(op.f("ck_entity_relations_source_valid"), "entity_relations", type_="check")
    op.create_check_constraint(
        op.f("ck_entity_relations_source_valid"),
        "entity_relations",
        "source IN ('inferred','manual','traces')",
    )
    for enum in (incident_status, incident_severity):
        enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "incidents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("number", sa.Integer(), sa.Identity(always=True), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", incident_status, nullable=False),
        sa.Column("severity", incident_severity, nullable=False),
        _ts("started_at", default=False),
        _ts("detected_at", default=False),
        _ts("last_signal_at", default=False),
        _ts("mitigated_at", nullable=True, default=False),
        _ts("resolved_at", nullable=True, default=False),
        _ts("closed_at", nullable=True, default=False),
        sa.Column("primary_entity_id", sa.UUID(), nullable=True),
        sa.Column("service_name", sa.Text(), nullable=True),
        sa.Column("suspected_deployment_id", sa.UUID(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), server_default="system", nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.ForeignKeyConstraint(
            ["primary_entity_id"], ["entities.id"],
            name="fk_incidents_primary_entity_id_entities", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["suspected_deployment_id"], ["deployments.id"],
            name="fk_incidents_suspected_deployment_id_deployments", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_incidents"),
        sa.UniqueConstraint("number", name="uq_incidents_number"),
    )  # fmt: skip
    op.create_index("ix_incidents_status_last_signal_at", "incidents", ["status", "last_signal_at"])
    op.create_index(
        "ix_incidents_service_name_started_at", "incidents", ["service_name", "started_at"]
    )
    op.create_index("ix_incidents_started_at", "incidents", ["started_at"])

    op.create_table(
        "incident_entities",
        sa.Column("incident_id", sa.UUID(), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        _ts("added_at"),
        sa.CheckConstraint("role IN ('origin','affected')", name="ck_incident_entities_role_valid"),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["incidents.id"],
            name="fk_incident_entities_incident_id_incidents", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"], ["entities.id"],
            name="fk_incident_entities_entity_id_entities", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("incident_id", "entity_id", name="pk_incident_entities"),
    )  # fmt: skip
    op.create_index("ix_incident_entities_entity_id", "incident_entities", ["entity_id"])

    op.create_table(
        "incident_signals",
        sa.Column("incident_id", sa.UUID(), nullable=False),
        sa.Column("signal_id", sa.UUID(), nullable=False),
        _ts("attached_at"),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.CheckConstraint(f"reason IN {_in(ATTACH_REASONS)}", name="ck_incident_signals_reason_valid"),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["incidents.id"],
            name="fk_incident_signals_incident_id_incidents", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["signal_id"], ["signals.id"],
            name="fk_incident_signals_signal_id_signals", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("incident_id", "signal_id", name="pk_incident_signals"),
        sa.UniqueConstraint("signal_id", name="uq_incident_signals_signal_id"),
    )  # fmt: skip

    op.create_table(
        "incident_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("incident_id", sa.UUID(), nullable=False),
        _ts("ts"),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "data", postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.CheckConstraint(f"type IN {_in(EVENT_TYPES)}", name="ck_incident_events_type_valid"),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["incidents.id"],
            name="fk_incident_events_incident_id_incidents", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_incident_events"),
    )  # fmt: skip
    op.create_index("ix_incident_events_incident_id_ts", "incident_events", ["incident_id", "ts"])

    # The timeline is append-only: no UPDATE for any role (like the audit log).
    op.execute(
        """
        CREATE FUNCTION incident_events_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'incident_events is append-only';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER incident_events_no_update BEFORE UPDATE ON incident_events "
        "FOR EACH ROW EXECUTE FUNCTION incident_events_append_only()"
    )
    # And the app may only add to it (cascades from `incidents` run as the owner).
    op.execute(
        _IF_ROLE.format(
            stmt="REVOKE UPDATE, DELETE, TRUNCATE ON incident_events FROM sentinel_app;"
        )
    )


def downgrade() -> None:
    op.execute("DELETE FROM entity_relations WHERE source = 'traces'")
    op.drop_constraint(op.f("ck_entity_relations_source_valid"), "entity_relations", type_="check")
    op.create_check_constraint(
        op.f("ck_entity_relations_source_valid"),
        "entity_relations",
        "source IN ('inferred','manual')",
    )
    op.execute("DROP TRIGGER IF EXISTS incident_events_no_update ON incident_events")
    op.execute("DROP FUNCTION IF EXISTS incident_events_append_only()")
    for table in ("incident_events", "incident_signals", "incident_entities", "incidents"):
        op.drop_table(table)
    for enum in (incident_severity, incident_status):
        enum.drop(op.get_bind(), checkfirst=True)
