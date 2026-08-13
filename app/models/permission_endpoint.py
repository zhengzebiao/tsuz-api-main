from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PermissionEndpoint(Base):
    __tablename__ = "permission_endpoints"
    __table_args__ = (
        Index(
            "ix_permission_endpoints_http_method_path",
            "http_method",
            "path",
        ),
    )

    permission_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    http_method: Mapped[str] = mapped_column(String(16), primary_key=True)
    path: Mapped[str] = mapped_column(String(2048), primary_key=True)
    route_name: Mapped[str] = mapped_column(String(255))
