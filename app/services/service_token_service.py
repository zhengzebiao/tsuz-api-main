from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.core.config import settings
from app.core.security import parse_pem_key, verify_app_secret
from app.models.app import App
from app.services.app_service_grant_service import AppServiceGrantService


class ServiceTokenError(ValueError):
    code = "service_token_error"


class InvalidClientError(ServiceTokenError):
    code = "invalid_client"


class InvalidTargetError(ServiceTokenError):
    code = "invalid_target"


class InvalidScopeError(ServiceTokenError):
    code = "invalid_scope"


class ServiceTokenConfigurationError(ServiceTokenError):
    code = "server_error"


class ServiceTokenService:
    def __init__(self, db: DbSession) -> None:
        self.db = db

    @property
    def expires_in_seconds(self) -> int:
        return settings.service_token_expire_seconds

    def authenticate_client(self, app_id: str, app_secret: str) -> App:
        app = self.db.scalar(select(App).where(App.app_id == app_id))
        if app is None or not app.is_enabled or not verify_app_secret(app_secret, app.app_secret_hash):
            raise InvalidClientError(InvalidClientError.code)
        return app

    def issue_token(
        self,
        *,
        caller: App,
        audience: str,
        requested_scopes: set[str],
    ) -> tuple[str, str]:
        target = self.db.scalar(select(App).where(App.app_id == audience))
        if target is None or not target.is_enabled:
            raise InvalidTargetError(InvalidTargetError.code)
        allowed_scopes = AppServiceGrantService(self.db).effective_scopes(
            caller_app_id=caller.app_id,
            target_app_id=target.app_id,
        )
        if not requested_scopes or not requested_scopes.issubset(allowed_scopes):
            raise InvalidScopeError(InvalidScopeError.code)
        normalized_scope = " ".join(sorted(requested_scopes))
        return self._encode(caller.app_id, target.app_id, normalized_scope), normalized_scope

    def _encode(self, subject: str, audience: str, scope: str) -> str:
        if not settings.jwt_private_key or not settings.service_token_issuer:
            raise ServiceTokenConfigurationError(ServiceTokenConfigurationError.code)
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=self.expires_in_seconds)
        payload = {
            "iss": settings.service_token_issuer,
            "sub": subject,
            "aud": audience,
            "token_use": "service",
            "scope": scope,
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "jti": str(uuid4()),
        }
        return jwt.encode(
            payload,
            parse_pem_key(settings.jwt_private_key),
            algorithm=settings.jwt_algorithm,
        )
