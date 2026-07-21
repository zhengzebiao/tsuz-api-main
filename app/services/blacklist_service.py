from datetime import datetime, timezone

from app.core.config import settings
from app.core.redis import get_redis


class BlacklistService:
    def add_jti(self, jti: str, exp: int) -> None:
        ttl = max(1, exp - int(datetime.now(timezone.utc).timestamp()))
        get_redis().setex(f"{settings.token_blacklist_prefix}{jti}", ttl, "1")

    def ensure_not_blacklisted(self, jti: str) -> None:
        if get_redis().exists(f"{settings.token_blacklist_prefix}{jti}"):
            raise ValueError("token is blacklisted")
