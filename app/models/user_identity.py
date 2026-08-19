from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, false, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class UserIdentity(Base):
    __tablename__ = "user_identities"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_subject",
            name="uq_user_identities_provider_provider_subject",
        ),
        Index("ix_user_identities_user_id_provider", "user_id", "provider"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32))
    provider_subject: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(128))
    avatar: Mapped[str | None] = mapped_column(String(2048))
    verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        onupdate=func.now(),
        server_default=func.now(),
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
