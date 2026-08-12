from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, func, text, true
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class App(Base):
    __tablename__ = "apps"
    __table_args__ = (
        Index("ix_apps_name", "name"),
        Index("ix_apps_is_enabled", "is_enabled"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    app_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    app_secret_hash: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    icon_url: Mapped[str | None] = mapped_column(String(2048))
    access_url: Mapped[str] = mapped_column(String(2048))
    service_account_name: Mapped[str] = mapped_column(String(128))
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime)
    disabled_reason: Mapped[str | None] = mapped_column(String(500))
    secret_updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        onupdate=func.now(),
        server_default=func.now(),
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
