"""add app service authorization

Revision ID: 0007_app_service_authorization
Revises: 0006_email_registration
Create Date: 2026-09-11
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_app_service_authorization"
down_revision: str | None = "0006_email_registration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "resource_scopes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("target_app_id", sa.String(length=64), nullable=False),
        sa.Column("scope_code", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=255), server_default="", nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["target_app_id"], ["apps.app_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("target_app_id", "scope_code", name="uq_resource_scopes_target_scope"),
    )
    op.create_index("ix_resource_scopes_id", "resource_scopes", ["id"])
    op.create_index("ix_resource_scopes_target_app_id", "resource_scopes", ["target_app_id"])
    op.create_index("ix_resource_scopes_scope_code", "resource_scopes", ["scope_code"])
    op.create_index("ix_resource_scopes_is_enabled", "resource_scopes", ["is_enabled"])
    op.create_index(
        "ix_resource_scopes_target_enabled",
        "resource_scopes",
        ["target_app_id", "is_enabled"],
    )

    op.create_table(
        "app_service_grants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("caller_app_id", sa.String(length=64), nullable=False),
        sa.Column("scope_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="enabled", nullable=False),
        sa.Column("valid_from", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_by", sa.Integer(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoke_reason", sa.String(length=500), nullable=True),
        sa.CheckConstraint("status IN ('enabled', 'revoked')", name="ck_app_service_grants_status"),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at > valid_from",
            name="ck_app_service_grants_valid_window",
        ),
        sa.ForeignKeyConstraint(["caller_app_id"], ["apps.app_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["scope_id"], ["resource_scopes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["revoked_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("caller_app_id", "scope_id", name="uq_app_service_grants_caller_scope"),
    )
    op.create_index("ix_app_service_grants_id", "app_service_grants", ["id"])
    op.create_index("ix_app_service_grants_caller_app_id", "app_service_grants", ["caller_app_id"])
    op.create_index("ix_app_service_grants_scope_id", "app_service_grants", ["scope_id"])
    op.create_index("ix_app_service_grants_status", "app_service_grants", ["status"])
    op.create_index("ix_app_service_grants_created_by", "app_service_grants", ["created_by"])
    op.create_index("ix_app_service_grants_revoked_by", "app_service_grants", ["revoked_by"])
    op.create_index(
        "ix_app_service_grants_caller_status",
        "app_service_grants",
        ["caller_app_id", "status"],
    )
    op.create_index(
        "ix_app_service_grants_scope_status",
        "app_service_grants",
        ["scope_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_app_service_grants_scope_status", table_name="app_service_grants")
    op.drop_index("ix_app_service_grants_caller_status", table_name="app_service_grants")
    op.drop_index("ix_app_service_grants_revoked_by", table_name="app_service_grants")
    op.drop_index("ix_app_service_grants_created_by", table_name="app_service_grants")
    op.drop_index("ix_app_service_grants_status", table_name="app_service_grants")
    op.drop_index("ix_app_service_grants_scope_id", table_name="app_service_grants")
    op.drop_index("ix_app_service_grants_caller_app_id", table_name="app_service_grants")
    op.drop_index("ix_app_service_grants_id", table_name="app_service_grants")
    op.drop_table("app_service_grants")

    op.drop_index("ix_resource_scopes_target_enabled", table_name="resource_scopes")
    op.drop_index("ix_resource_scopes_is_enabled", table_name="resource_scopes")
    op.drop_index("ix_resource_scopes_scope_code", table_name="resource_scopes")
    op.drop_index("ix_resource_scopes_target_app_id", table_name="resource_scopes")
    op.drop_index("ix_resource_scopes_id", table_name="resource_scopes")
    op.drop_table("resource_scopes")
