from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.core.config import settings
from app.core.redis import get_redis
from app.models.session import Session as AuthSession


class SessionService:
    def __init__(self, db: DbSession | None = None) -> None:
        self.db = db

    def revoke_session(self, sid: str, reason: str = "user_logout") -> None:
        if self.db is not None:
            auth_session = self.db.scalar(select(AuthSession).where(AuthSession.sid == sid))
            if auth_session is not None and auth_session.status == "active":
                self._revoke_db_session(auth_session, reason)
                self.db.flush()
        self._write_redis_revocation(sid)

    def revoke_user_sessions(self, user_id: int, reason: str) -> int:
        if self.db is None:
            raise RuntimeError("database session is required")
        sessions = self.db.scalars(
            select(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.status == "active")
            .order_by(AuthSession.id)
            .with_for_update()
        ).all()
        for auth_session in sessions:
            self._revoke_db_session(auth_session, reason)
        self.db.flush()
        for auth_session in sessions:
            self._write_redis_revocation(auth_session.sid)
        return len(sessions)

    def ensure_session_active(self, sid: str) -> None:
        if get_redis().get(f"{settings.session_prefix}{sid}") == "revoked":
            raise ValueError("session is revoked")
        if self.db is None:
            return
        auth_session = self.db.scalar(select(AuthSession).where(AuthSession.sid == sid))
        if auth_session is None or auth_session.status != "active":
            raise ValueError("session is revoked")

    def _revoke_db_session(self, auth_session: AuthSession, reason: str) -> None:
        auth_session.status = "revoked"
        auth_session.revoked_at = datetime.now(UTC).replace(tzinfo=None)
        auth_session.revoked_reason = reason

    def _write_redis_revocation(self, sid: str) -> None:
        ttl = settings.refresh_token_expire_days * 24 * 60 * 60
        get_redis().set(f"{settings.session_prefix}{sid}", "revoked", ex=ttl)
