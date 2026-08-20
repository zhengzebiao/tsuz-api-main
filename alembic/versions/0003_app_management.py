"""add app management model

Revision ID: 0003_app_management
Revises: 0002_user_management
Create Date: 2026-08-12
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_app_management"
down_revision: str | None = "0002_user_management"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "apps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("app_id", sa.String(length=64), nullable=False),
        sa.Column("app_secret_hash", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("icon_url", sa.String(length=2048), nullable=True),
        sa.Column("access_url", sa.String(length=2048), nullable=False),
        sa.Column("service_account_name", sa.String(length=128), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("disabled_at", sa.DateTime(), nullable=True),
        sa.Column("disabled_reason", sa.String(length=500), nullable=True),
        sa.Column("secret_updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.create_index("ix_apps_id", "apps", ["id"])
    op.create_index("ix_apps_app_id", "apps", ["app_id"], unique=True)
    op.create_index("ix_apps_name", "apps", ["name"])
    op.create_index("ix_apps_is_enabled", "apps", ["is_enabled"])


def downgrade() -> None:
    op.drop_index("ix_apps_is_enabled", table_name="apps")
    op.drop_index("ix_apps_name", table_name="apps")
    op.drop_index("ix_apps_app_id", table_name="apps")
    op.drop_index("ix_apps_id", table_name="apps")
    op.drop_table("apps")
