import logging
from datetime import datetime, timedelta, timezone
from secrets import token_urlsafe
from typing import Any

from sqlalchemy.orm import Session as DbSession

from app.core.config import settings
from app.core.security import sha256_text
from app.core.redis import get_redis
from app.services.session_service import SessionService

logger = logging.getLogger("app.auth.refresh")


class RefreshTokenReuseError(ValueError):
    def __init__(self, sid: str) -> None:
        super().__init__("refresh token reuse detected")
        self.sid = sid


class RefreshTokenService:
    def __init__(self, db: DbSession | None = None) -> None:
        self.sessions = SessionService(db)

    def create_refresh_token(self, user_id: str, sid: str) -> str:
        refresh_token = token_urlsafe(48)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=settings.refresh_token_expire_days)
        key = self._refresh_key(refresh_token)
        get_redis().hset(
            key,
            mapping={
                "user_id": user_id,
                "sid": sid,
                "status": "active",
                "created_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
                "rotated_at": "",
                "replaced_by": "",
            },
        )
        get_redis().expire(key, settings.refresh_token_expire_days * 24 * 60 * 60)
        return refresh_token

    def rotate_refresh_token(self, refresh_token: str) -> dict[str, Any]:
        key = self._refresh_key(refresh_token)
        data = get_redis().hgetall(key)
        if not data:
            logger.warning("refresh rejected reason=missing_token_hash")
            raise ValueError("invalid refresh token")

        sid = data.get("sid", "")
        if sid:
            self.ensure_session_active(sid)
        if self._is_expired(data.get("expires_at", "")):
            get_redis().hset(key, mapping={"status": "revoked"})
            logger.warning("refresh rejected reason=expired sid=%s", sid)
            raise ValueError("invalid refresh token")

        status = data.get("status")
        if status == "active":
            user_id = data["user_id"]
            sid = data["sid"]
            new_refresh_token = self.create_refresh_token(user_id=user_id, sid=sid)
            new_refresh_hash = sha256_text(new_refresh_token)
            get_redis().hset(
                key,
                mapping={
                    "status": "rotated",
                    "rotated_at": datetime.now(timezone.utc).isoformat(),
                    "replaced_by": new_refresh_hash,
                },
            )
            logger.info("refresh rotated sid=%s replaced_by=%s", sid, new_refresh_hash[:12])
            return {"user_id": user_id, "sid": sid, "refresh_token": new_refresh_token}

        if status == "rotated":
            if self._within_reuse_grace(data.get("rotated_at", "")):
                logger.warning("refresh replay within grace sid=%s", sid)
                raise ValueError("refresh token already rotated")
            if sid:
                self.revoke_session(sid, reason="refresh_token_reuse")
            logger.warning("refresh reuse detected sid=%s", sid)
            raise RefreshTokenReuseError(sid)

        logger.warning("refresh rejected reason=status_%s sid=%s", status, sid)
        raise ValueError("invalid refresh token")

    def revoke_session(self, sid: str, reason: str = "user_logout") -> None:
        self.sessions.revoke_session(sid, reason)

    def ensure_session_active(self, sid: str) -> None:
        self.sessions.ensure_session_active(sid)

    def _refresh_key(self, refresh_token: str) -> str:
        return f"{settings.refresh_token_prefix}{sha256_text(refresh_token)}"

    def _is_expired(self, expires_at: str) -> bool:
        expires = self._parse_timestamp(expires_at)
        return expires is None or expires <= datetime.now(timezone.utc)

    def _within_reuse_grace(self, rotated_at: str) -> bool:
        rotated = self._parse_timestamp(rotated_at)
        if rotated is None:
            return False
        elapsed = datetime.now(timezone.utc) - rotated
        return elapsed <= timedelta(seconds=settings.refresh_token_reuse_grace_seconds)

    def _parse_timestamp(self, value: str) -> datetime | None:
        if not value:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
