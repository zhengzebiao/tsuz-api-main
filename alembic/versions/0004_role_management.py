"""add role management fields

Revision ID: 0004_role_management
Revises: 0003_app_management
Create Date: 2026-08-12
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_role_management"
down_revision: str | None = "0003_app_management"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "roles",
        sa.Column("description", sa.String(length=255), server_default="", nullable=True),
    )
    op.add_column(
        "roles",
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.true(), nullable=True),
    )
    op.add_column("roles", sa.Column("disabled_at", sa.DateTime(), nullable=True))
    op.add_column("roles", sa.Column("disabled_reason", sa.String(length=500), nullable=True))
    op.add_column(
        "roles",
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
    )
    op.add_column(
        "roles",
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
    )
    op.add_column(
        "roles",
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE roles
            SET description = COALESCE(description, ''),
                is_enabled = COALESCE(is_enabled, true),
                created_at = COALESCE(created_at, CURRENT_TIMESTAMP),
                updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP),
                version = COALESCE(version, 1)
            WHERE description IS NULL
               OR is_enabled IS NULL
               OR created_at IS NULL
               OR updated_at IS NULL
               OR version IS NULL
            """
        )
    )
    op.alter_column("roles", "description", existing_type=sa.String(length=255), nullable=False)
    op.alter_column("roles", "is_enabled", existing_type=sa.Boolean(), nullable=False)
    op.alter_column("roles", "created_at", existing_type=sa.DateTime(), nullable=False)
    op.alter_column("roles", "updated_at", existing_type=sa.DateTime(), nullable=False)
    op.alter_column("roles", "version", existing_type=sa.Integer(), nullable=False)
    op.create_index("ix_roles_is_enabled", "roles", ["is_enabled"])


def downgrade() -> None:
    op.drop_index("ix_roles_is_enabled", table_name="roles")
    op.drop_column("roles", "version")
    op.drop_column("roles", "updated_at")
    op.drop_column("roles", "created_at")
    op.drop_column("roles", "disabled_reason")
    op.drop_column("roles", "disabled_at")
    op.drop_column("roles", "is_enabled")
    op.drop_column("roles", "description")
