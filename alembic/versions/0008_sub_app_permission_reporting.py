"""add sub-application permission ownership

Revision ID: 0008_permission_reporting
Revises: 0007_app_service_authorization
Create Date: 2026-09-13
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0008_permission_reporting"
down_revision: str | None = "0007_app_service_authorization"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "permissions",
        sa.Column(
            "owner_app_id",
            sa.String(length=64),
            sa.ForeignKey("apps.app_id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_permissions_owner_app_id",
        "permissions",
        ["owner_app_id"],
    )
    op.create_index(
        "ix_permissions_owner_app_declared",
        "permissions",
        ["owner_app_id", "is_declared"],
    )


def downgrade() -> None:
    op.drop_index("ix_permissions_owner_app_declared", table_name="permissions")
    op.drop_index("ix_permissions_owner_app_id", table_name="permissions")
    op.drop_column("permissions", "owner_app_id")
