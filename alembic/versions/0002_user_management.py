"""add user management models

Revision ID: 0002_user_management
Revises: 0001_initial_auth_schema
Create Date: 2026-08-11
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_user_management"
down_revision: str | None = "0001_initial_auth_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("display_name", sa.String(length=128), nullable=True))
    op.add_column(
        "users",
        sa.Column("is_blacklisted", sa.Boolean(), server_default=sa.false(), nullable=True),
    )
    op.add_column("users", sa.Column("disabled_at", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("disabled_reason", sa.String(length=500), nullable=True))
    op.add_column("users", sa.Column("blacklisted_at", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("blacklisted_reason", sa.String(length=500), nullable=True))
    op.add_column("users", sa.Column("password_changed_at", sa.DateTime(), nullable=True))
    op.add_column(
        "users",
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE users
            SET is_blacklisted = COALESCE(is_blacklisted, false),
                created_at = COALESCE(created_at, CURRENT_TIMESTAMP),
                updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP),
                version = COALESCE(version, 1)
            WHERE is_blacklisted IS NULL
               OR created_at IS NULL
               OR updated_at IS NULL
               OR version IS NULL
            """
        )
    )
    op.alter_column("users", "is_blacklisted", existing_type=sa.Boolean(), nullable=False)
    op.alter_column("users", "created_at", existing_type=sa.DateTime(), nullable=False)
    op.alter_column("users", "updated_at", existing_type=sa.DateTime(), nullable=False)
    op.alter_column("users", "version", existing_type=sa.Integer(), nullable=False)
    op.create_index(
        "ix_users_is_active_is_blacklisted",
        "users",
        ["is_active", "is_blacklisted"],
    )

    op.add_column("sessions", sa.Column("revoked_at", sa.DateTime(), nullable=True))
    op.add_column("sessions", sa.Column("revoked_reason", sa.String(length=64), nullable=True))
    op.create_index("ix_sessions_user_id_status", "sessions", ["user_id", "status"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("changes_json", sa.JSON(), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
    )
    op.create_index("ix_audit_events_id", "audit_events", ["id"])
    op.create_index("ix_audit_events_actor_user_id", "audit_events", ["actor_user_id"])
    op.create_index("ix_audit_events_action", "audit_events", ["action"])
    op.create_index("ix_audit_events_target", "audit_events", ["target_type", "target_id"])
    op.create_index("ix_audit_events_request_id", "audit_events", ["request_id"])
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_index("ix_audit_events_request_id", table_name="audit_events")
    op.drop_index("ix_audit_events_target", table_name="audit_events")
    op.drop_index("ix_audit_events_action", table_name="audit_events")
    op.drop_index("ix_audit_events_actor_user_id", table_name="audit_events")
    op.drop_index("ix_audit_events_id", table_name="audit_events")
    op.drop_table("audit_events")

    op.drop_index("ix_sessions_user_id_status", table_name="sessions")
    op.drop_column("sessions", "revoked_reason")
    op.drop_column("sessions", "revoked_at")

    op.drop_index("ix_users_is_active_is_blacklisted", table_name="users")
    op.drop_column("users", "version")
    op.drop_column("users", "updated_at")
    op.drop_column("users", "created_at")
    op.drop_column("users", "password_changed_at")
    op.drop_column("users", "blacklisted_reason")
    op.drop_column("users", "blacklisted_at")
    op.drop_column("users", "disabled_reason")
    op.drop_column("users", "disabled_at")
    op.drop_column("users", "is_blacklisted")
    op.drop_column("users", "display_name")
