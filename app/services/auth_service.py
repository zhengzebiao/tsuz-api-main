import logging
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.core.security import verify_password
from app.models.permission import Permission
from app.models.role import Role, role_permissions, user_roles
from app.models.session import Session as AuthSession
from app.models.user import User
from app.schemas.auth import LoginRequest, LogoutResponse, TokenResponse, UserResponse
from app.services.authorization_service import AuthenticationError, ensure_user_can_authenticate
from app.services.blacklist_service import BlacklistService
from app.services.refresh_token_service import RefreshTokenReuseError, RefreshTokenService
from app.services.token_service import TokenService

logger = logging.getLogger("app.auth")


class AuthService:
    def __init__(self, db: DbSession) -> None:
        self.db = db
        self.tokens = TokenService()
        self.refresh_tokens = RefreshTokenService(db)
        self.sessions = self.refresh_tokens.sessions
        self.blacklist = BlacklistService()

    def login(self, payload: LoginRequest) -> TokenResponse:
        email = str(payload.username).lower()
        user = self._get_user_by_email(email)
        try:
            user = ensure_user_can_authenticate(user)
        except AuthenticationError as exc:
            logger.warning("login failed username=%s reason=invalid_credentials", email)
            raise ValueError("invalid credentials") from exc
        if not verify_password(payload.password, user.hashed_password):
            logger.warning("login failed username=%s reason=invalid_credentials", email)
            raise ValueError("invalid credentials")

        user = self._lock_authenticatable_user(user.id)
        sid = self._create_db_session(user)
        roles, scope = self._build_claims(user)
        access_token = self.tokens.create_access_token(
            user_id=str(user.id), sid=sid, roles=roles, scope=scope
        )
        refresh_token = self.refresh_tokens.create_refresh_token(user_id=str(user.id), sid=sid)
        self.db.commit()
        logger.info("login succeeded user_id=%s", user.id)
        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=self.tokens.expires_in_seconds,
        )

    def refresh(self, refresh_token: str) -> TokenResponse:
        try:
            rotation = self.refresh_tokens.rotate_refresh_token(refresh_token)
        except RefreshTokenReuseError as exc:
            if exc.sid:
                self.sessions.revoke_session(exc.sid, reason="refresh_token_reuse")
                self.db.commit()
            raise ValueError("refresh token reuse detected") from exc
        try:
            user = self._get_authenticatable_user_by_id(rotation["user_id"])
        except AuthenticationError as exc:
            self.sessions.revoke_session(rotation["sid"], reason="authentication_state_changed")
            self.db.commit()
            raise ValueError("invalid user") from exc
        try:
            self.sessions.ensure_session_active(rotation["sid"])
        except ValueError as exc:
            self.sessions.revoke_session(rotation["sid"], reason="authentication_state_changed")
            self.db.commit()
            raise ValueError("session is revoked") from exc
        roles, scope = self._build_claims(user)
        access_token = self.tokens.create_access_token(
            user_id=str(user.id), sid=rotation["sid"], roles=roles, scope=scope
        )
        logger.info("refresh succeeded user_id=%s", user.id)
        return TokenResponse(
            access_token=access_token,
            refresh_token=rotation["refresh_token"],
            expires_in=self.tokens.expires_in_seconds,
        )

    def logout(self, access_token: str) -> LogoutResponse:
        payload = self.tokens.verify_access_token(access_token)
        jti = self._required_string_claim(payload, "jti")
        sid = self._required_string_claim(payload, "sid")
        exp = payload.get("exp")
        if not isinstance(exp, int):
            raise ValueError("invalid access token")
        self.blacklist.add_jti(jti, exp)
        self.sessions.revoke_session(sid, reason="user_logout")
        self.db.commit()
        logger.info("logout succeeded user_session_revoked=true")
        return LogoutResponse(message="logged out")

    def current_user(self, access_token: str) -> UserResponse:
        payload = self.tokens.verify_access_token(access_token)
        user_id = self._required_string_claim(payload, "sub")
        jti = self._required_string_claim(payload, "jti")
        sid = self._required_string_claim(payload, "sid")
        self.blacklist.ensure_not_blacklisted(jti)
        self.sessions.ensure_session_active(sid)
        user = self._get_authenticatable_user_by_id(user_id)
        roles, _scope = self._build_claims(user)
        return UserResponse(id=str(user.id), username=user.email, roles=roles)

    def _get_user_by_email(self, email: str) -> User | None:
        return self.db.scalar(select(User).where(User.email == email))

    def _get_authenticatable_user_by_id(self, user_id: str | int) -> User:
        try:
            numeric_user_id = int(user_id)
        except (TypeError, ValueError) as exc:
            raise AuthenticationError("invalid user") from exc
        return ensure_user_can_authenticate(self.db.get(User, numeric_user_id))

    def _lock_authenticatable_user(self, user_id: int) -> User:
        user = self.db.scalar(select(User).where(User.id == user_id).with_for_update())
        return ensure_user_can_authenticate(user)

    def _get_user_roles(self, user_id: int) -> list[str]:
        roles = self.db.scalars(
            select(Role.name)
            .join(user_roles, user_roles.c.role_id == Role.id)
            .where(
                user_roles.c.user_id == user_id,
                Role.is_enabled.is_(True),
            )
            .order_by(Role.name)
        ).all()
        return list(roles)

    def _get_user_permissions(self, user_id: int) -> list[str]:
        permissions = self.db.scalars(
            select(Permission.name)
            .distinct()
            .join(role_permissions, role_permissions.c.permission_id == Permission.id)
            .join(Role, Role.id == role_permissions.c.role_id)
            .join(user_roles, user_roles.c.role_id == Role.id)
            .where(
                user_roles.c.user_id == user_id,
                Role.is_enabled.is_(True),
            )
            .order_by(Permission.name)
        ).all()
        return list(permissions)

    def _build_claims(self, user: User) -> tuple[list[str], str]:
        roles = self._get_user_roles(user.id)
        permissions = self._get_user_permissions(user.id)
        return roles, " ".join(permissions)

    def _create_db_session(self, user: User) -> str:
        sid = uuid4().hex
        self.db.add(AuthSession(sid=sid, user_id=user.id, status="active"))
        self.db.flush()
        return sid

    def _required_string_claim(self, payload: dict, claim: str) -> str:
        value = payload.get(claim)
        if not isinstance(value, str) or not value:
            raise ValueError("invalid access token")
        return value
