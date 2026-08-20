"""add email registration fields

Revision ID: 0006_email_registration
Revises: 0005_permission_management
Create Date: 2026-08-14
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_email_registration"
down_revision: str | None = "0005_permission_management"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(), nullable=True))
    op.execute(
        sa.text(
            """
            UPDATE users
            SET email_verified_at = created_at
            WHERE email IS NOT NULL
              AND email_verified_at IS NULL
            """
        )
    )


def downgrade() -> None:
    op.drop_column("users", "email_verified_at")
