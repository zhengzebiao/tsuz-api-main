from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func, true
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ResourceScope(Base):
    __tablename__ = "resource_scopes"
    __table_args__ = (
        UniqueConstraint("target_app_id", "scope_code", name="uq_resource_scopes_target_scope"),
        Index("ix_resource_scopes_target_enabled", "target_app_id", "is_enabled"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    target_app_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("apps.app_id", ondelete="RESTRICT"),
        index=True,
    )
    scope_code: Mapped[str] = mapped_column(String(128), index=True)
    description: Mapped[str] = mapped_column(String(255), default="", server_default="")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        onupdate=func.now(),
        server_default=func.now(),
    )
