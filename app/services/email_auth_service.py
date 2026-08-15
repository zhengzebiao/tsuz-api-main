from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session as DbSession

from app.core.security import hash_password
from app.models.role import Role, user_roles
from app.models.user import User
from app.schemas.auth import (
    EmailChallengeResponse,
    PasswordForgotCodeResponse,
    PasswordResetResponse,
    TokenResponse,
)
from app.services.auth_service import AuthService
from app.services.authorization_service import AuthenticationError, ensure_user_can_authenticate
from app.services.tencent_ses_service import EmailProviderError, TencentSesService
from app.services.verification_challenge_service import (
    ChallengeNotFoundError,
    ChallengeStateError,
    CreatedChallenge,
    VerificationChallengeService,
)

logger = logging.getLogger("app.auth.email")


class EmailAuthError(ValueError):
    """Base class for email authentication business failures."""


class EmailAlreadyRegisteredError(EmailAuthError):
    """The submitted email already belongs to a user."""


class EmailPasswordPolicyError(EmailAuthError):
    """The submitted password does not meet the configured policy."""


class EmailAuthConfigurationError(RuntimeError):
    """A required role or other server-side dependency is unavailable."""


class EmailAuthStateError(RuntimeError):
    """Email authentication state could not be persisted."""


class EmailAuthService:
    NORMAL_ROLE_NAME = "normal"
    PASSWORD_MIN_LENGTH = 10
    PASSWORD_MAX_LENGTH = 128
    PASSWORD_RESET_MESSAGE = "密码重置成功，请使用新密码登录"
    PASSWORD_FORGOT_MESSAGE = "如果邮箱已注册，验证码将发送到该邮箱"

    def __init__(
        self,
        db: DbSession,
        *,
        challenges: VerificationChallengeService | None = None,
        email_provider: TencentSesService | None = None,
        auth: AuthService | None = None,
    ) -> None:
        self.db = db
        self.challenges = (
            challenges if challenges is not None else VerificationChallengeService()
        )
        self.email_provider = email_provider
        self.auth = auth if auth is not None else AuthService(db)
        self.sessions = self.auth.sessions

    def send_registration_code(self, email: str, client_ip: str) -> EmailChallengeResponse:
        normalized_email = self.normalize_email(email)
        created = self.challenges.create_challenge(normalized_email, "register", client_ip)
        self._send_challenge(normalized_email, created, "register")
        return self._challenge_response(created)

    def register(
        self,
        email: str,
        challenge_id: str,
        code: str,
        password: str,
    ) -> TokenResponse:
        normalized_email = self.normalize_email(email)
        self._validate_password(password)
        self.challenges.consume_challenge(challenge_id, normalized_email, "register", code)

        try:
            role = self.db.scalar(
                select(Role)
                .where(
                    Role.name == self.NORMAL_ROLE_NAME,
                    Role.is_enabled.is_(True),
                )
                .with_for_update()
            )
            if role is None:
                raise EmailAuthConfigurationError("email registration is unavailable")
            if self.db.scalar(select(User.id).where(User.email == normalized_email)) is not None:
                raise EmailAlreadyRegisteredError("email registration failed")

            user = User(
                email=normalized_email,
                hashed_password=hash_password(password),
                is_active=True,
                is_blacklisted=False,
                email_verified_at=self._now(),
            )
            try:
                with self.db.begin_nested():
                    self.db.add(user)
                    self.db.flush()
            except IntegrityError as exc:
                raise EmailAlreadyRegisteredError("email registration failed") from exc
            self.db.execute(user_roles.insert().values(user_id=user.id, role_id=role.id))
            self.db.commit()
        except (EmailAlreadyRegisteredError, EmailAuthConfigurationError):
            self.db.rollback()
            raise
        except SQLAlchemyError as exc:
            self.db.rollback()
            logger.error("email registration persistence failed")
            raise EmailAuthStateError("email authentication unavailable") from exc

        return self.auth.complete_login(user.id)

    def login(self, email: str, password: str) -> TokenResponse:
        return self.auth.login_by_email(self.normalize_email(email), password)

    def send_password_reset_code(self, email: str, client_ip: str) -> PasswordForgotCodeResponse:
        normalized_email = self.normalize_email(email)
        user = self.db.scalar(select(User).where(User.email == normalized_email))
        try:
            eligible_user = ensure_user_can_authenticate(user)
        except AuthenticationError:
            eligible_user = None

        created = self.challenges.create_challenge(normalized_email, "password_reset", client_ip)
        if eligible_user is None:
            self.challenges.delete_challenge(created.challenge_id)
        else:
            self._send_challenge(normalized_email, created, "password_reset")
        return PasswordForgotCodeResponse(
            message=self.PASSWORD_FORGOT_MESSAGE,
            challenge_id=created.challenge_id,
            expires_in=created.expires_in,
            resend_after=created.resend_after,
        )

    def reset_password(
        self,
        email: str,
        challenge_id: str,
        code: str,
        new_password: str,
    ) -> PasswordResetResponse:
        normalized_email = self.normalize_email(email)
        self._validate_password(new_password)
        user = self.db.scalar(
            select(User).where(User.email == normalized_email).with_for_update()
        )
        try:
            user = ensure_user_can_authenticate(user)
        except AuthenticationError as exc:
            self.db.rollback()
            raise ChallengeNotFoundError("verification challenge is invalid or expired") from exc

        self.challenges.consume_challenge(
            challenge_id,
            normalized_email,
            "password_reset",
            code,
        )
        try:
            user.hashed_password = hash_password(new_password)
            user.password_changed_at = self._now()
            user.version += 1
            user.updated_at = self._now()
            self.sessions.revoke_user_sessions(user.id, "password_reset")
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            logger.error("password reset persistence failed")
            raise EmailAuthStateError("email authentication unavailable") from exc
        return PasswordResetResponse(message=self.PASSWORD_RESET_MESSAGE)

    @staticmethod
    def normalize_email(email: str) -> str:
        return str(email).strip().lower()

    def _send_challenge(
        self,
        email: str,
        created: CreatedChallenge,
        purpose: str,
    ) -> None:
        try:
            provider = (
                self.email_provider
                if self.email_provider is not None
                else TencentSesService()
            )
            provider.send_verification_email(email, created.code, purpose=purpose)
        except EmailProviderError:
            try:
                self.challenges.delete_challenge(created.challenge_id)
            except ChallengeStateError:
                logger.warning("challenge cleanup failed after provider error")
            raise

    def _challenge_response(self, created: CreatedChallenge) -> EmailChallengeResponse:
        return EmailChallengeResponse(
            challenge_id=created.challenge_id,
            expires_in=created.expires_in,
            resend_after=created.resend_after,
        )

    def _validate_password(self, password: str) -> None:
        if not self.PASSWORD_MIN_LENGTH <= len(password) <= self.PASSWORD_MAX_LENGTH:
            raise EmailPasswordPolicyError("invalid password")

    def _now(self) -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)
