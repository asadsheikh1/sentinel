"""Auth: session idle tracking, invites, nullable password, readonly column limits.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IF_ROLE = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
    {stmt}
  END IF;
END
$$;
"""

# Every users column except password_hash.
_USERS_READONLY_COLUMNS = (
    "id, email, display_name, role, is_active, last_login_at, failed_login_count, "
    "locked_until, created_at, updated_at"
)


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # Invited users have no password until they accept the invite.
    op.alter_column("users", "password_hash", existing_type=sa.Text(), nullable=True)

    op.create_table(
        "user_invites",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "char_length(token_hash) = 64", name=op.f("ck_user_invites_token_hash_sha256_hex")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_invites_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_user_invites_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_invites")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_user_invites_token_hash")),
    )
    op.create_index(op.f("ix_user_invites_user_id"), "user_invites", ["user_id"])

    # sentinel_readonly: no invite token hashes, and no password hashes.
    op.execute(
        _IF_ROLE.format(
            role="sentinel_readonly",
            stmt=(
                "REVOKE ALL ON user_invites FROM sentinel_readonly; "
                "REVOKE SELECT ON users FROM sentinel_readonly; "
                f"GRANT SELECT ({_USERS_READONLY_COLUMNS}) ON users TO sentinel_readonly;"
            ),
        )
    )


def downgrade() -> None:
    op.execute(
        _IF_ROLE.format(
            role="sentinel_readonly",
            stmt=(
                f"REVOKE SELECT ({_USERS_READONLY_COLUMNS}) ON users FROM sentinel_readonly; "
                "GRANT SELECT ON users TO sentinel_readonly;"
            ),
        )
    )
    op.drop_index(op.f("ix_user_invites_user_id"), table_name="user_invites")
    op.drop_table("user_invites")
    # Users without a password can't be represented by 0001; give them an unusable marker.
    op.execute("UPDATE users SET password_hash = '!' WHERE password_hash IS NULL")
    op.alter_column("users", "password_hash", existing_type=sa.Text(), nullable=False)
    op.drop_column("sessions", "last_seen_at")
