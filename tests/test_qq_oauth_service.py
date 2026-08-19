from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from redis.exceptions import RedisError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.database import Base
from app.core.security import sha256_text
from app.models.role import Role, user_roles
from app.models.session import Session as AuthSession
from app.models.user import User
from app.models.user_identity import UserIdentity
from app.services.authorization_service import AuthenticationError
from app.services.qq_oauth_service import (
    QQOAuthConfigurationError,
    QQOAuthProviderError,
    QQOAuthService,
    QQOAuthStateError,
    QQOAuthTicketError,
    QQProfile,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}
        self.lock = threading.Lock()

    def set(self, key: str, value: str, ex: int | None = None, nx: bool = False):
        with self.lock:
            if nx and key in self.values:
                return False
            self.values[key] = str(value)
            if ex is not None:
                self.expirations[key] = ex
            return True

    def eval(self, _script: str, numkeys: int, *args):
        assert numkeys == 1
        key = args[0]
        with self.lock:
            value = self.values.pop(key, None)
            self.expirations.pop(key, None)
            return value


class FailingRedis:
    def set(self, *_args, **_kwargs):
        raise RedisError("redis unavailable")

    def eval(self, *_args, **_kwargs):
        raise RedisError("redis unavailable")


class FakeHTTP:
    def __init__(self, responses: list[httpx.Response] | None = None, error: Exception | None = None):
        self.responses = list(responses or [])
        self.error = error
        self.calls: list[tuple[str, dict, int]] = []

    def __call__(self, url: str, *, params: dict, timeout: int):
        self.calls.append((url, params, timeout))
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


@pytest.fixture
def oauth_settings() -> Settings:
    return Settings(
        _env_file=None,
        app_id="configured-app-id",
        app_key="configured-app-key",
        qq_redirect_uri="https://api.example.test/auth/qq/callback",
        qq_ticket_redirect_uri="https://web.example.test/login",
        qq_state_prefix="auth:test:qq:state:",
        qq_ticket_prefix="auth:test:qq:ticket:",
        qq_state_ttl_seconds=300,
        qq_ticket_ttl_seconds=60,
        qq_http_timeout_seconds=7,
    )


@pytest.fixture
def db_session() -> Iterator[DbSession]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def response(text: str, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, text=text, request=httpx.Request("GET", "https://qq.test"))


def make_service(
    db: DbSession,
    oauth_settings: Settings,
    redis: FakeRedis | None = None,
    http: FakeHTTP | None = None,
) -> tuple[QQOAuthService, FakeRedis, FakeHTTP]:
    fake_redis = redis or FakeRedis()
    fake_http = http or FakeHTTP()
    return QQOAuthService(db, oauth_settings, fake_redis, fake_http), fake_redis, fake_http


def add_normal(db: DbSession, *, enabled: bool = True) -> Role:
    role = Role(name="normal", is_enabled=enabled)
    db.add(role)
    db.commit()
    return role


def test_build_authorize_url_stores_hashed_state_and_fixed_callback(
    db_session: DbSession, oauth_settings: Settings
) -> None:
    service, redis, _http = make_service(db_session, oauth_settings)

    authorize_url = service.build_authorize_url()
    query = parse_qs(urlparse(authorize_url).query)
    state = query["state"][0]

    assert query["response_type"] == ["code"]
    assert query["client_id"] == [oauth_settings.app_id]
    assert query["redirect_uri"] == [oauth_settings.qq_redirect_uri]
    assert query["scope"] == ["get_user_info"]
    assert len(state) >= 40
    assert state not in str(redis.values)
    assert f"{oauth_settings.qq_state_prefix}{sha256_text(state)}" in redis.values
    assert redis.values[f"{oauth_settings.qq_state_prefix}{sha256_text(state)}"] == "pending"
    assert redis.expirations[f"{oauth_settings.qq_state_prefix}{sha256_text(state)}"] == 300
    assert oauth_settings.app_key not in authorize_url


def test_missing_configuration_fails_before_state_storage(
    db_session: DbSession, oauth_settings: Settings
) -> None:
    oauth_settings.app_id = ""
    service, redis, _http = make_service(db_session, oauth_settings)

    with pytest.raises(QQOAuthConfigurationError, match="QQ OAuth is not configured"):
        service.build_authorize_url()

    assert redis.values == {}


def test_state_is_consumed_once_and_concurrent_consumers_have_one_success(
    db_session: DbSession, oauth_settings: Settings
) -> None:
    service, _redis, _http = make_service(db_session, oauth_settings)
    state = service.build_authorize_url()
    state = parse_qs(urlparse(state).query)["state"][0]

    def consume() -> str:
        try:
            service._consume_state(state)
        except QQOAuthStateError:
            return "failed"
        return "success"

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _index: consume(), range(8)))

    assert results.count("success") == 1
    assert results.count("failed") == 7
    with pytest.raises(QQOAuthStateError):
        service._consume_state(state)


def test_ticket_is_hashed_minimal_and_consumed_once(
    db_session: DbSession, oauth_settings: Settings
) -> None:
    service, redis, _http = make_service(db_session, oauth_settings)

    ticket = service.issue_ticket(42)
    key = f"{oauth_settings.qq_ticket_prefix}{sha256_text(ticket)}"

    assert ticket not in str(redis.values)
    assert redis.values[key] == "42"
    assert redis.expirations[key] == 60
    assert service.consume_ticket(ticket) == 42
    with pytest.raises(QQOAuthTicketError):
        service.consume_ticket(ticket)


def test_ticket_consumption_rejects_non_positive_or_non_integer_values(
    db_session: DbSession, oauth_settings: Settings
) -> None:
    service, redis, _http = make_service(db_session, oauth_settings)
    for value in ("0", "-1", "not-an-id"):
        ticket = "ticket-" + value
        redis.values[service._ticket_key(ticket)] = value
        with pytest.raises(QQOAuthTicketError):
            service.consume_ticket(ticket)


def test_redis_errors_are_mapped_to_safe_state_and_ticket_failures(
    db_session: DbSession, oauth_settings: Settings
) -> None:
    service = QQOAuthService(db_session, oauth_settings, FailingRedis(), FakeHTTP())

    with pytest.raises(QQOAuthStateError, match="invalid or expired QQ OAuth state"):
        service.build_authorize_url()
    with pytest.raises(QQOAuthStateError, match="invalid or expired QQ OAuth state"):
        service._consume_state("state")
    with pytest.raises(QQOAuthTicketError, match="invalid or expired QQ ticket"):
        service.issue_ticket(1)
    with pytest.raises(QQOAuthTicketError, match="invalid or expired QQ ticket"):
        service.consume_ticket("ticket")


def test_token_parser_accepts_json_and_query_string(
    db_session: DbSession, oauth_settings: Settings
) -> None:
    assert QQOAuthService._parse_token_response('{"access_token":"token-value","expires_in":60}')[
        "access_token"
    ] == "token-value"
    assert QQOAuthService._parse_token_response("access_token=token-value&expires_in=60")["access_token"] == "token-value"


def test_openid_parser_accepts_json_and_strict_jsonp(
    db_session: DbSession, oauth_settings: Settings
) -> None:
    service, _redis, _http = make_service(db_session, oauth_settings)
    assert service._parse_json_or_jsonp('{"client_id":"configured-app-id","openid":"subject"}') == {
        "client_id": "configured-app-id",
        "openid": "subject",
    }
    assert service._parse_json_or_jsonp('callback({"client_id":"configured-app-id","openid":"subject"});')[
        "openid"
    ] == "subject"
    with pytest.raises(QQOAuthProviderError):
        service._parse_json_or_jsonp('evil({"client_id":"configured-app-id","openid":"subject"});')


def test_openid_requires_exact_client_id_and_nonempty_subject(
    db_session: DbSession, oauth_settings: Settings
) -> None:
    for payload in (
        {"client_id": "CONFIGURED-APP-ID", "openid": "subject"},
        {"client_id": "configured-app-id", "openid": ""},
    ):
        http = FakeHTTP([response(json.dumps({"access_token": "token"})), response(json.dumps(payload))])
        service, _redis, _http = make_service(db_session, oauth_settings, http=http)
        state_url = service.build_authorize_url()
        state = parse_qs(urlparse(state_url).query)["state"][0]
        with pytest.raises(QQOAuthProviderError):
            service.complete_authorization("authorization-code", state)


def test_http_errors_are_safe_and_timeout_is_forwarded(
    db_session: DbSession, oauth_settings: Settings
) -> None:
    http = FakeHTTP(error=httpx.ReadTimeout("secret provider detail"))
    service, _redis, _http = make_service(db_session, oauth_settings, http=http)

    with pytest.raises(QQOAuthProviderError) as exc_info:
        service._request_token("authorization-code-secret")

    assert str(exc_info.value) == "QQ OAuth provider unavailable"
    assert "secret" not in str(exc_info.value)


def test_complete_authorization_creates_qq_only_user_identity_role_and_ticket(
    db_session: DbSession, oauth_settings: Settings
) -> None:
    add_normal(db_session)
    http = FakeHTTP(
        [
            response('{"access_token":"access-token-secret","expires_in":60}'),
            response('{"client_id":"configured-app-id","openid":"qq-subject-secret"}'),
            response('{"ret":0,"nickname":"QQ Name","figureurl_qq_2":"https://avatar.test/a"}'),
        ]
    )
    service, redis, _http = make_service(db_session, oauth_settings, http=http)
    state_url = service.build_authorize_url()
    state = parse_qs(urlparse(state_url).query)["state"][0]

    ticket = service.complete_authorization("authorization-code-secret", state)

    user = db_session.scalar(select(User).where(User.email.is_(None)))
    assert user is not None
    identity = db_session.scalar(select(UserIdentity).where(UserIdentity.user_id == user.id))
    assert identity is not None
    assert identity.provider == "qq"
    assert identity.provider_subject == "qq-subject-secret"
    assert identity.display_name == "QQ Name"
    assert db_session.scalar(select(user_roles.c.role_id).where(user_roles.c.user_id == user.id)) is not None
    assert service.consume_ticket(ticket) == user.id
    assert db_session.scalar(select(AuthSession).where(AuthSession.user_id == user.id)) is None
    redis_material = str(redis.values)
    assert "access-token-secret" not in redis_material
    assert "qq-subject-secret" not in redis_material


def test_existing_identity_updates_profile_without_overwriting_local_name(
    db_session: DbSession, oauth_settings: Settings
) -> None:
    user = User(email=None, hashed_password=None, display_name="Local Name")
    identity = UserIdentity(
        user_id=1,
        provider="qq",
        provider_subject="subject",
        display_name="Old",
        avatar="https://avatar.test/old",
    )
    db_session.add(user)
    db_session.flush()
    identity.user_id = user.id
    db_session.add(identity)
    db_session.commit()

    service, _redis, _http = make_service(db_session, oauth_settings)
    result = service.upsert_identity(QQProfile("subject", "New QQ Name", "https://avatar.test/new"))

    assert result.id == user.id
    db_session.refresh(user)
    db_session.refresh(identity)
    assert user.display_name == "Local Name"
    assert identity.display_name == "New QQ Name"
    assert identity.avatar == "https://avatar.test/new"
    assert identity.verified is True
    assert identity.last_login_at is not None


def test_inactive_or_blacklisted_existing_identity_is_rejected(
    db_session: DbSession, oauth_settings: Settings
) -> None:
    for active, blacklisted in ((False, False), (True, True)):
        user = User(email=None, hashed_password=None, is_active=active, is_blacklisted=blacklisted)
        db_session.add(user)
        db_session.flush()
        db_session.add(UserIdentity(user_id=user.id, provider="qq", provider_subject=f"subject-{user.id}"))
        db_session.commit()
        service, _redis, _http = make_service(db_session, oauth_settings)
        with pytest.raises(AuthenticationError, match="invalid user"):
            service.upsert_identity(QQProfile(f"subject-{user.id}", "Name", None))


def test_missing_or_disabled_normal_role_rolls_back_user_and_identity(
    db_session: DbSession, oauth_settings: Settings
) -> None:
    add_normal(db_session, enabled=False)
    service, _redis, _http = make_service(db_session, oauth_settings)

    with pytest.raises(QQOAuthConfigurationError):
        service.upsert_identity(QQProfile("missing-role-subject", "Name", None))

    assert db_session.scalar(select(User).where(User.email.is_(None))) is None
    assert db_session.scalar(select(UserIdentity).where(UserIdentity.provider_subject == "missing-role-subject")) is None


def test_profile_fields_are_bounded(
    db_session: DbSession, oauth_settings: Settings
) -> None:
    profile = QQOAuthService._normalize_profile(
        QQProfile("subject", "n" * 200, "a" * 3000)
    )
    assert len(profile.display_name or "") == 128
    assert len(profile.avatar or "") == 2048
    assert len(profile.provider_subject) == len("subject")
