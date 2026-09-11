from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AppServiceGrant(Base):
    __tablename__ = "app_service_grants"
    __table_args__ = (
        UniqueConstraint("caller_app_id", "scope_id", name="uq_app_service_grants_caller_scope"),
        CheckConstraint("status IN ('enabled', 'revoked')", name="ck_app_service_grants_status"),
        CheckConstraint(
            "expires_at IS NULL OR expires_at > valid_from",
            name="ck_app_service_grants_valid_window",
        ),
        Index("ix_app_service_grants_caller_status", "caller_app_id", "status"),
        Index("ix_app_service_grants_scope_status", "scope_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    caller_app_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("apps.app_id", ondelete="RESTRICT"),
        index=True,
    )
    scope_id: Mapped[int] = mapped_column(
        ForeignKey("resource_scopes.id", ondelete="RESTRICT"),
        index=True,
    )
    status: Mapped[str] = mapped_column(String(16), default="enabled", server_default="enabled", index=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime, default=func.now(), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), server_default=func.now())
    revoked_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoke_reason: Mapped[str | None] = mapped_column(String(500))
