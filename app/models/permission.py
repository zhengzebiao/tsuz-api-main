from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, func, text, true
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Permission(Base):
    __tablename__ = "permissions"
    __table_args__ = (
        Index("ix_permissions_is_declared", "is_declared"),
        Index("ix_permissions_is_enabled", "is_enabled"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128), default="", server_default="")
    description: Mapped[str] = mapped_column(String(255), default="", server_default="")
    is_declared: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime)
    disabled_reason: Mapped[str | None] = mapped_column(String(500))
    missing_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        onupdate=func.now(),
        server_default=func.now(),
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
