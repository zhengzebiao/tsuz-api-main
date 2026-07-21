from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt

from app.core.config import settings
from app.core.security import parse_pem_key


class TokenService:
    @property
    def expires_in_seconds(self) -> int:
        return settings.access_token_expire_minutes * 60

    def create_access_token(self, user_id: str, sid: str, roles: list[str], scope: str) -> str:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=settings.access_token_expire_minutes)
        payload = {
            "sub": user_id,
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "jti": str(uuid4()),
            "sid": sid,
            "roles": roles,
            "scope": scope,
        }
        return jwt.encode(payload, parse_pem_key(settings.jwt_private_key), algorithm=settings.jwt_algorithm)

    def verify_access_token(self, token: str) -> dict:
        return jwt.decode(
            token,
            parse_pem_key(settings.jwt_public_key),
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
        )
