"""add QQ OAuth identity support

Revision ID: 0007_qq_login
Revises: 0006_email_registration
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_qq_login"
down_revision: str | None = "0006_email_registration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "users",
        "email",
        existing_type=sa.String(length=320),
        nullable=True,
    )
    op.alter_column(
        "users",
        "hashed_password",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.create_table(
        "user_identities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_subject", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=True),
        sa.Column("avatar", sa.String(length=2048), nullable=True),
        sa.Column("verified", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "provider",
            "provider_subject",
            name="uq_user_identities_provider_provider_subject",
        ),
    )
    op.create_index("ix_user_identities_id", "user_identities", ["id"])
    op.create_index("ix_user_identities_user_id", "user_identities", ["user_id"])
    op.create_index(
        "ix_user_identities_user_id_provider",
        "user_identities",
        ["user_id", "provider"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    qq_only_count = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM users
            WHERE email IS NULL OR hashed_password IS NULL
            """
        )
    ).scalar_one()
    if qq_only_count:
        raise RuntimeError("cannot downgrade 0007_qq_login while QQ-only users have NULL email or hashed_password")

    op.drop_index("ix_user_identities_user_id_provider", table_name="user_identities")
    op.drop_index("ix_user_identities_user_id", table_name="user_identities")
    op.drop_index("ix_user_identities_id", table_name="user_identities")
    op.drop_table("user_identities")
    op.alter_column(
        "users",
        "hashed_password",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.alter_column(
        "users",
        "email",
        existing_type=sa.String(length=320),
        nullable=False,
    )
