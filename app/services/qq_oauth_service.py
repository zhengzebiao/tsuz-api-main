from __future__ import annotations

import json
import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlencode

import httpx
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session as DbSession

from app.core.config import Settings, settings
from app.core.redis import get_redis
from app.core.security import sha256_text
from app.models.role import Role, user_roles
from app.models.user import User
from app.models.user_identity import UserIdentity
from app.services.authorization_service import AuthenticationError, ensure_user_can_authenticate

logger = logging.getLogger("app.auth.qq")

HttpGet = Callable[..., httpx.Response]


class QQOAuthError(ValueError):
    """Base class for safe QQ OAuth business failures."""


class QQOAuthConfigurationError(QQOAuthError):
    """QQ OAuth configuration is incomplete or invalid."""


class QQOAuthProviderError(QQOAuthError):
    """The QQ provider response could not be trusted or used."""


class QQOAuthStateError(QQOAuthError):
    """The one-time OAuth state is missing, expired, or unavailable."""


class QQOAuthTicketError(QQOAuthError):
    """The one-time ticket is missing, expired, malformed, or unavailable."""


class QQOAuthIdentityError(QQOAuthError):
    """The QQ identity could not be persisted safely."""


@dataclass(frozen=True)
class QQProfile:
    provider_subject: str
    display_name: str | None
    avatar: str | None
    verified: bool = True


class QQOAuthService:
    """Implement the internal QQ OAuth boundary without issuing local tokens."""

    PROVIDER = "qq"
    NORMAL_ROLE_NAME = "normal"
    STATE_VALUE = "pending"
    STATE_ERROR_MESSAGE = "invalid or expired QQ OAuth state"
    TICKET_ERROR_MESSAGE = "invalid or expired QQ ticket"
    CONFIGURATION_ERROR_MESSAGE = "QQ OAuth is not configured"
    PROVIDER_ERROR_MESSAGE = "QQ OAuth provider unavailable"
    IDENTITY_ERROR_MESSAGE = "QQ identity unavailable"

    _CONSUME_SCRIPT = """
local value = redis.call('GET', KEYS[1])
if not value then
    return false
end
redis.call('DEL', KEYS[1])
return value
"""
    _JSONP_RE = re.compile(
        r"^\s*callback\s*\(\s*(\{.*\})\s*\)\s*;?\s*$",
        re.DOTALL,
    )
    _POSITIVE_INTEGER_RE = re.compile(r"^[1-9][0-9]*$")
    _MAX_SET_ATTEMPTS = 3

    def __init__(
        self,
        db: DbSession,
        configured_settings: Settings | None = None,
        redis_client: object | None = None,
        http_client: HttpGet | object | None = None,
    ) -> None:
        self.db = db
        self.settings = configured_settings or settings
        self.redis = redis_client if redis_client is not None else get_redis()
        self.http_client = http_client if http_client is not None else httpx.get

    def build_authorize_url(self) -> str:
        self._validate_authorize_configuration()
        state = self._create_state()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.settings.app_id,
                "redirect_uri": self.settings.qq_redirect_uri,
                "scope": "get_user_info",
                "state": state,
            }
        )
        separator = "&" if "?" in self.settings.qq_authorize_url else "?"
        return f"{self.settings.qq_authorize_url}{separator}{query}"

    def complete_authorization(self, code: str, state: str) -> str:
        self._validate_provider_configuration()
        if not isinstance(code, str) or not code.strip():
            raise QQOAuthProviderError(self.PROVIDER_ERROR_MESSAGE)
        if not isinstance(state, str) or not state:
            raise QQOAuthStateError(self.STATE_ERROR_MESSAGE)

        self._consume_state(state)
        access_token = self._request_token(code)
        openid = self._request_openid(access_token)
        profile = self._request_profile(access_token, openid)
        user = self.upsert_identity(profile)
        return self.issue_ticket(user.id)

    def issue_ticket(self, user_id: int) -> str:
        self._validate_ticket_configuration()
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise QQOAuthTicketError(self.TICKET_ERROR_MESSAGE)

        for _attempt in range(self._MAX_SET_ATTEMPTS):
            ticket = secrets.token_urlsafe(48)
            try:
                created = self.redis.set(
                    self._ticket_key(ticket),
                    str(user_id),
                    ex=self.settings.qq_ticket_ttl_seconds,
                    nx=True,
                )
            except (AttributeError, OSError, RedisError, TypeError, ValueError):
                logger.error("qq ticket storage failed")
                raise QQOAuthTicketError(self.TICKET_ERROR_MESSAGE) from None
            if created:
                return ticket
        raise QQOAuthTicketError(self.TICKET_ERROR_MESSAGE)

    def consume_ticket(self, ticket: str) -> int:
        self._validate_ticket_configuration()
        if not isinstance(ticket, str) or not ticket:
            raise QQOAuthTicketError(self.TICKET_ERROR_MESSAGE)
        try:
            value = self.redis.eval(self._CONSUME_SCRIPT, 1, self._ticket_key(ticket))
        except (AttributeError, OSError, RedisError, TypeError, ValueError):
            logger.error("qq ticket consumption failed")
            raise QQOAuthTicketError(self.TICKET_ERROR_MESSAGE) from None
        return self._parse_consumed_user_id(value)

    def upsert_identity(self, profile: QQProfile) -> User:
        profile = self._normalize_profile(profile)
        try:
            identity = self.db.scalar(
                select(UserIdentity)
                .where(
                    UserIdentity.provider == self.PROVIDER,
                    UserIdentity.provider_subject == profile.provider_subject,
                )
                .with_for_update()
            )
            if identity is not None:
                return self._update_existing_identity(identity, profile)
            return self._create_identity(profile)
        except (QQOAuthError, AuthenticationError):
            self.db.rollback()
            raise
        except SQLAlchemyError:
            self.db.rollback()
            logger.error("qq identity persistence failed")
            raise QQOAuthIdentityError(self.IDENTITY_ERROR_MESSAGE) from None

    def _update_existing_identity(self, identity: UserIdentity, profile: QQProfile) -> User:
        user = self._lock_user(identity.user_id)
        ensure_user_can_authenticate(user)
        self._apply_profile(identity, user, profile)
        self.db.commit()
        return user

    def _create_identity(self, profile: QQProfile) -> User:
        role = self.db.scalar(
            select(Role)
            .where(
                Role.name == self.NORMAL_ROLE_NAME,
                Role.is_enabled.is_(True),
            )
            .with_for_update()
        )
        if role is None:
            raise QQOAuthConfigurationError(self.CONFIGURATION_ERROR_MESSAGE)

        user = User(
            email=None,
            hashed_password=None,
            display_name=profile.display_name,
            is_active=True,
            is_blacklisted=False,
        )
        try:
            with self.db.begin_nested():
                self.db.add(user)
                self.db.flush()
                identity = UserIdentity(
                    user_id=user.id,
                    provider=self.PROVIDER,
                    provider_subject=profile.provider_subject,
                    display_name=profile.display_name,
                    avatar=profile.avatar,
                    verified=profile.verified,
                    last_login_at=self._now(),
                )
                self.db.add(identity)
                self.db.flush()
                self.db.execute(
                    user_roles.insert().values(user_id=user.id, role_id=role.id)
                )
        except IntegrityError:
            return self._recover_identity_conflict(profile)

        self.db.commit()
        return user

    def _recover_identity_conflict(self, profile: QQProfile) -> User:
        identity = self.db.scalar(
            select(UserIdentity)
            .where(
                UserIdentity.provider == self.PROVIDER,
                UserIdentity.provider_subject == profile.provider_subject,
            )
            .with_for_update()
        )
        if identity is None:
            logger.error("qq identity uniqueness conflict could not be recovered")
            raise QQOAuthIdentityError(self.IDENTITY_ERROR_MESSAGE)
        user = self._lock_user(identity.user_id)
        ensure_user_can_authenticate(user)
        self._apply_profile(identity, user, profile)
        self.db.commit()
        return user

    def _lock_user(self, user_id: int) -> User | None:
        return self.db.scalar(select(User).where(User.id == user_id).with_for_update())

    def _apply_profile(self, identity: UserIdentity, user: User, profile: QQProfile) -> None:
        identity.display_name = profile.display_name
        identity.avatar = profile.avatar
        identity.verified = profile.verified
        identity.last_login_at = self._now()
        if not user.display_name and profile.display_name:
            user.display_name = profile.display_name

    def _request_token(self, code: str) -> str:
        response = self._get(
            self.settings.qq_token_url,
            params={
                "grant_type": "authorization_code",
                "client_id": self.settings.app_id,
                "client_secret": self.settings.app_key,
                "code": code,
                "redirect_uri": self.settings.qq_redirect_uri,
            },
        )
        payload = self._parse_token_response(self._response_text(response))
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise QQOAuthProviderError(self.PROVIDER_ERROR_MESSAGE)
        return access_token.strip()

    def _request_openid(self, access_token: str) -> str:
        response = self._get(
            self.settings.qq_openid_url,
            params={"access_token": access_token},
        )
        payload = self._parse_json_or_jsonp(self._response_text(response))
        self._raise_for_provider_error(payload)
        client_id = payload.get("client_id")
        openid = payload.get("openid")
        if client_id != self.settings.app_id:
            raise QQOAuthProviderError(self.PROVIDER_ERROR_MESSAGE)
        if not isinstance(openid, str) or not openid.strip():
            raise QQOAuthProviderError(self.PROVIDER_ERROR_MESSAGE)
        return openid.strip()

    def _request_profile(self, access_token: str, openid: str) -> QQProfile:
        response = self._get(
            self.settings.qq_user_info_url,
            params={
                "access_token": access_token,
                "oauth_consumer_key": self.settings.app_id,
                "openid": openid,
            },
        )
        payload = self._parse_json_object(self._response_text(response))
        self._raise_for_provider_error(payload, require_ret_zero=True)
        return QQProfile(
            provider_subject=openid,
            display_name=self._bounded_optional_string(payload.get("nickname"), 128),
            avatar=self._first_bounded_string(
                payload,
                ("figureurl_qq_2", "figureurl_qq_1", "figureurl", "avatar"),
                2048,
            ),
        )

    def _get(self, url: str, *, params: dict[str, str]) -> httpx.Response:
        try:
            requester = self.http_client
            if callable(requester):
                response = requester(
                    url,
                    params=params,
                    timeout=self.settings.qq_http_timeout_seconds,
                )
            else:
                response = requester.get(
                    url,
                    params=params,
                    timeout=self.settings.qq_http_timeout_seconds,
                )
        except (httpx.TimeoutException, httpx.RequestError):
            raise QQOAuthProviderError(self.PROVIDER_ERROR_MESSAGE) from None
        status_code = getattr(response, "status_code", None)
        if not isinstance(status_code, int) or not 200 <= status_code < 300:
            raise QQOAuthProviderError(self.PROVIDER_ERROR_MESSAGE)
        return response

    @staticmethod
    def _response_text(response: httpx.Response) -> str:
        try:
            text = response.text
        except (AttributeError, OSError, TypeError, ValueError):
            raise QQOAuthProviderError(QQOAuthService.PROVIDER_ERROR_MESSAGE) from None
        if not isinstance(text, str) or not text.strip():
            raise QQOAuthProviderError(QQOAuthService.PROVIDER_ERROR_MESSAGE)
        return text

    @classmethod
    def _parse_token_response(cls, text: str) -> dict:
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            try:
                parsed = parse_qs(text, keep_blank_values=True, strict_parsing=True)
            except ValueError:
                raise QQOAuthProviderError(cls.PROVIDER_ERROR_MESSAGE) from None
            if not parsed or any(len(values) != 1 or not values[0] for values in parsed.values()):
                raise QQOAuthProviderError(cls.PROVIDER_ERROR_MESSAGE)
            payload = {key: values[0] for key, values in parsed.items()}
        if not isinstance(payload, dict):
            raise QQOAuthProviderError(cls.PROVIDER_ERROR_MESSAGE)
        cls._raise_for_provider_error(payload)
        return payload

    @classmethod
    def _parse_json_or_jsonp(cls, text: str) -> dict:
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            match = cls._JSONP_RE.fullmatch(text)
            if match is None:
                raise QQOAuthProviderError(cls.PROVIDER_ERROR_MESSAGE)
            try:
                payload = json.loads(match.group(1))
            except (TypeError, ValueError):
                raise QQOAuthProviderError(cls.PROVIDER_ERROR_MESSAGE) from None
        if not isinstance(payload, dict):
            raise QQOAuthProviderError(cls.PROVIDER_ERROR_MESSAGE)
        return payload

    @classmethod
    def _parse_json_object(cls, text: str) -> dict:
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            raise QQOAuthProviderError(cls.PROVIDER_ERROR_MESSAGE) from None
        if not isinstance(payload, dict):
            raise QQOAuthProviderError(cls.PROVIDER_ERROR_MESSAGE)
        return payload

    @classmethod
    def _raise_for_provider_error(cls, payload: dict, *, require_ret_zero: bool = False) -> None:
        if payload.get("error") not in (None, "", 0):
            raise QQOAuthProviderError(cls.PROVIDER_ERROR_MESSAGE)
        if payload.get("error_description") not in (None, ""):
            raise QQOAuthProviderError(cls.PROVIDER_ERROR_MESSAGE)
        if require_ret_zero and "ret" in payload and payload["ret"] != 0:
            raise QQOAuthProviderError(cls.PROVIDER_ERROR_MESSAGE)
        if not require_ret_zero and payload.get("ret") not in (None, 0):
            raise QQOAuthProviderError(cls.PROVIDER_ERROR_MESSAGE)

    def _create_state(self) -> str:
        self._validate_state_configuration()
        for _attempt in range(self._MAX_SET_ATTEMPTS):
            state = secrets.token_urlsafe(32)
            try:
                created = self.redis.set(
                    self._state_key(state),
                    self.STATE_VALUE,
                    ex=self.settings.qq_state_ttl_seconds,
                    nx=True,
                )
            except (AttributeError, OSError, RedisError, TypeError, ValueError):
                logger.error("qq OAuth state storage failed")
                raise QQOAuthStateError(self.STATE_ERROR_MESSAGE) from None
            if created:
                return state
        raise QQOAuthStateError(self.STATE_ERROR_MESSAGE)

    def _consume_state(self, state: str) -> None:
        self._validate_state_configuration()
        try:
            value = self.redis.eval(self._CONSUME_SCRIPT, 1, self._state_key(state))
        except (AttributeError, OSError, RedisError, TypeError, ValueError):
            logger.error("qq OAuth state consumption failed")
            raise QQOAuthStateError(self.STATE_ERROR_MESSAGE) from None
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if value != self.STATE_VALUE:
            raise QQOAuthStateError(self.STATE_ERROR_MESSAGE)

    def _state_key(self, state: str) -> str:
        return f"{self.settings.qq_state_prefix}{sha256_text(state)}"

    def _ticket_key(self, ticket: str) -> str:
        return f"{self.settings.qq_ticket_prefix}{sha256_text(ticket)}"

    def _validate_authorize_configuration(self) -> None:
        self._validate_nonempty_strings(
            self.settings.app_id,
            self.settings.qq_redirect_uri,
            self.settings.qq_authorize_url,
        )
        self._validate_state_configuration()

    def _validate_provider_configuration(self) -> None:
        self._validate_authorize_configuration()
        self._validate_nonempty_strings(
            self.settings.app_key,
            self.settings.qq_token_url,
            self.settings.qq_openid_url,
            self.settings.qq_user_info_url,
        )
        self._validate_ticket_configuration()
        if self.settings.qq_http_timeout_seconds <= 0:
            raise QQOAuthConfigurationError(self.CONFIGURATION_ERROR_MESSAGE)

    def _validate_state_configuration(self) -> None:
        self._validate_nonempty_strings(self.settings.qq_state_prefix)
        if self.settings.qq_state_ttl_seconds <= 0:
            raise QQOAuthConfigurationError(self.CONFIGURATION_ERROR_MESSAGE)

    def _validate_ticket_configuration(self) -> None:
        self._validate_nonempty_strings(self.settings.qq_ticket_prefix)
        if self.settings.qq_ticket_ttl_seconds <= 0:
            raise QQOAuthConfigurationError(self.CONFIGURATION_ERROR_MESSAGE)

    @classmethod
    def _validate_nonempty_strings(cls, *values: object) -> None:
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise QQOAuthConfigurationError(cls.CONFIGURATION_ERROR_MESSAGE)

    @classmethod
    def _normalize_profile(cls, profile: QQProfile) -> QQProfile:
        if not isinstance(profile, QQProfile):
            raise QQOAuthProviderError(cls.PROVIDER_ERROR_MESSAGE)
        provider_subject = profile.provider_subject
        if (
            not isinstance(provider_subject, str)
            or not provider_subject.strip()
            or len(provider_subject.strip()) > 255
            or not isinstance(profile.verified, bool)
        ):
            raise QQOAuthProviderError(cls.PROVIDER_ERROR_MESSAGE)
        return QQProfile(
            provider_subject=provider_subject.strip(),
            display_name=cls._bounded_optional_string(profile.display_name, 128),
            avatar=cls._bounded_optional_string(profile.avatar, 2048),
            verified=profile.verified,
        )

    @staticmethod
    def _bounded_optional_string(value: object, limit: int) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip()[:limit]

    @classmethod
    def _first_bounded_string(
        cls,
        payload: dict,
        keys: tuple[str, ...],
        limit: int,
    ) -> str | None:
        for key in keys:
            value = cls._bounded_optional_string(payload.get(key), limit)
            if value:
                return value
        return None

    @classmethod
    def _parse_consumed_user_id(cls, value: object) -> int:
        if isinstance(value, bytes):
            try:
                value = value.decode("ascii")
            except UnicodeDecodeError:
                raise QQOAuthTicketError(cls.TICKET_ERROR_MESSAGE) from None
        if not isinstance(value, str) or cls._POSITIVE_INTEGER_RE.fullmatch(value) is None:
            raise QQOAuthTicketError(cls.TICKET_ERROR_MESSAGE)
        return int(value)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)
