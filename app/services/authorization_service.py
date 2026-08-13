from dataclasses import dataclass

import jwt
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.models.permission import Permission
from app.models.user import User
from app.services.blacklist_service import BlacklistService
from app.services.session_service import SessionService
from app.services.token_service import TokenService


class AuthenticationError(ValueError):
    pass


class PermissionDeniedError(ValueError):
    pass


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    user: User
    payload: dict


def ensure_user_can_authenticate(user: User | None) -> User:
    if user is None or not user.is_active or user.is_blacklisted:
        raise AuthenticationError("invalid user")
    return user


class AuthorizationService:
    def __init__(self, db: DbSession) -> None:
        self.db = db
        self.tokens = TokenService()
        self.blacklist = BlacklistService()
        self.sessions = SessionService(db)

    def authenticate_access_token(self, access_token: str) -> AuthenticatedPrincipal:
        try:
            payload = self.tokens.verify_access_token(access_token)
            user_id = self._required_integer_claim(payload, "sub")
            jti = self._required_string_claim(payload, "jti")
            sid = self._required_string_claim(payload, "sid")
            self.blacklist.ensure_not_blacklisted(jti)
            self.sessions.ensure_session_active(sid)
            user = ensure_user_can_authenticate(self.db.get(User, user_id))
        except (jwt.PyJWTError, ValueError, TypeError) as exc:
            raise AuthenticationError("invalid access token") from exc
        return AuthenticatedPrincipal(user=user, payload=payload)

    def require_permissions(self, access_token: str, required_permissions: tuple[str, ...]) -> User:
        principal = self.authenticate_access_token(access_token)
        scope = principal.payload.get("scope", "")
        if not isinstance(scope, str):
            raise AuthenticationError("invalid access token")
        granted_permissions = set(scope.split())
        required = set(required_permissions)
        if not required.issubset(granted_permissions):
            raise PermissionDeniedError("insufficient permissions")
        active_permission_count = self.db.scalar(
            select(func.count())
            .select_from(Permission)
            .where(
                Permission.name.in_(required),
                Permission.is_declared.is_(True),
                Permission.is_enabled.is_(True),
            )
        ) or 0
        if active_permission_count != len(required):
            raise PermissionDeniedError("insufficient permissions")
        return principal.user

    def _required_string_claim(self, payload: dict, claim: str) -> str:
        value = payload.get(claim)
        if not isinstance(value, str) or not value:
            raise AuthenticationError("invalid access token")
        return value

    def _required_integer_claim(self, payload: dict, claim: str) -> int:
        value = self._required_string_claim(payload, claim)
        try:
            return int(value)
        except ValueError as exc:
            raise AuthenticationError("invalid access token") from exc
