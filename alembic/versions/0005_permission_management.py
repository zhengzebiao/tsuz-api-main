"""add permission management fields and endpoint bindings

Revision ID: 0005_permission_management
Revises: 0004_role_management
Create Date: 2026-08-13
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_permission_management"
down_revision: str | None = "0004_role_management"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "permissions",
        sa.Column("display_name", sa.String(length=128), server_default="", nullable=True),
    )
    op.add_column(
        "permissions",
        sa.Column("is_declared", sa.Boolean(), server_default=sa.true(), nullable=True),
    )
    op.add_column(
        "permissions",
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.true(), nullable=True),
    )
    op.add_column("permissions", sa.Column("disabled_at", sa.DateTime(), nullable=True))
    op.add_column("permissions", sa.Column("disabled_reason", sa.String(length=500), nullable=True))
    op.add_column("permissions", sa.Column("missing_at", sa.DateTime(), nullable=True))
    op.add_column(
        "permissions",
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
    )
    op.add_column(
        "permissions",
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
    )
    op.add_column(
        "permissions",
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE permissions
            SET display_name = CASE
                    WHEN display_name IS NULL OR display_name = '' THEN name
                    ELSE display_name
                END,
                is_declared = COALESCE(is_declared, true),
                is_enabled = COALESCE(is_enabled, true),
                created_at = COALESCE(created_at, CURRENT_TIMESTAMP),
                updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP),
                version = COALESCE(version, 1)
            WHERE display_name IS NULL
               OR display_name = ''
               OR is_declared IS NULL
               OR is_enabled IS NULL
               OR created_at IS NULL
               OR updated_at IS NULL
               OR version IS NULL
            """
        )
    )
    op.alter_column(
        "permissions",
        "display_name",
        existing_type=sa.String(length=128),
        nullable=False,
    )
    op.alter_column("permissions", "is_declared", existing_type=sa.Boolean(), nullable=False)
    op.alter_column("permissions", "is_enabled", existing_type=sa.Boolean(), nullable=False)
    op.alter_column("permissions", "created_at", existing_type=sa.DateTime(), nullable=False)
    op.alter_column("permissions", "updated_at", existing_type=sa.DateTime(), nullable=False)
    op.alter_column("permissions", "version", existing_type=sa.Integer(), nullable=False)
    op.create_index("ix_permissions_is_declared", "permissions", ["is_declared"])
    op.create_index("ix_permissions_is_enabled", "permissions", ["is_enabled"])

    op.create_table(
        "permission_endpoints",
        sa.Column(
            "permission_id",
            sa.Integer(),
            sa.ForeignKey("permissions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("http_method", sa.String(length=16), primary_key=True),
        sa.Column("path", sa.String(length=2048), primary_key=True),
        sa.Column("route_name", sa.String(length=255), nullable=False),
    )
    op.create_index(
        "ix_permission_endpoints_http_method_path",
        "permission_endpoints",
        ["http_method", "path"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_permission_endpoints_http_method_path",
        table_name="permission_endpoints",
    )
    op.drop_table("permission_endpoints")
    op.drop_index("ix_permissions_is_enabled", table_name="permissions")
    op.drop_index("ix_permissions_is_declared", table_name="permissions")
    op.drop_column("permissions", "version")
    op.drop_column("permissions", "updated_at")
    op.drop_column("permissions", "created_at")
    op.drop_column("permissions", "missing_at")
    op.drop_column("permissions", "disabled_reason")
    op.drop_column("permissions", "disabled_at")
    op.drop_column("permissions", "is_enabled")
    op.drop_column("permissions", "is_declared")
    op.drop_column("permissions", "display_name")
