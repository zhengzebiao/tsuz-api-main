from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, false, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_is_active_is_blacklisted", "is_active", "is_blacklisted"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_blacklisted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime)
    disabled_reason: Mapped[str | None] = mapped_column(String(500))
    blacklisted_at: Mapped[datetime | None] = mapped_column(DateTime)
    blacklisted_reason: Mapped[str | None] = mapped_column(String(500))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        onupdate=func.now(),
        server_default=func.now(),
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
