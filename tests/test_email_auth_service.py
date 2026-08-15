from collections.abc import Iterator
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as DbSession, sessionmaker

import app.services.session_service as session_module
from app.core.database import Base
from app.core.security import hash_password, verify_password
from app.models.role import Role, user_roles
from app.models.session import Session as AuthSession
from app.models.user import User
from app.schemas.auth import TokenResponse
from app.services.email_auth_service import (
    EmailAlreadyRegisteredError,
    EmailAuthConfigurationError,
    EmailAuthService,
    EmailPasswordPolicyError,
)
from app.services.tencent_ses_service import EmailProviderError
from app.services.verification_challenge_service import ChallengeNotFoundError, CreatedChallenge


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value

    def get(self, key: str) -> str | None:
        return self.values.get(key)


class RecordingChallenges:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, str]] = []
        self.consumed: list[tuple[str, str, str, str]] = []
        self.deleted: list[str] = []
        self.next = CreatedChallenge(
            challenge_id="challenge-123",
            code="123456",
            expires_in=600,
            resend_after=60,
        )

    def create_challenge(self, email: str, purpose: str, client_ip: str) -> CreatedChallenge:
        self.created.append((email, purpose, client_ip))
        return self.next

    def consume_challenge(self, challenge_id: str, email: str, purpose: str, code: str) -> None:
        self.consumed.append((challenge_id, email, purpose, code))

    def delete_challenge(self, challenge_id: str) -> None:
        self.deleted.append(challenge_id)


class RecordingEmailProvider:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[tuple[str, str, str]] = []

    def send_verification_email(self, email: str, code: str, *, purpose: str):
        self.sent.append((email, code, purpose))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(message_id="message-1", request_id="request-1")


class RecordingAuthService:
    def __init__(self) -> None:
        self.completed: list[int] = []
        self.logins: list[tuple[str, str]] = []
        self.sessions = None

    def complete_login(self, user_id: int) -> TokenResponse:
        self.completed.append(user_id)
        return TokenResponse(access_token="access", refresh_token="refresh", expires_in=3600)

    def login_by_email(self, email: str, password: str) -> TokenResponse:
        self.logins.append((email, password))
        return TokenResponse(access_token="access", refresh_token="refresh", expires_in=3600)


@pytest.fixture
def db_session() -> Iterator[DbSession]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    redis = FakeRedis()
    monkeypatch.setattr(session_module, "get_redis", lambda: redis)
    return redis


def make_service(
    db: DbSession,
    *,
    challenges: RecordingChallenges | None = None,
    provider: RecordingEmailProvider | None = None,
    auth: RecordingAuthService | None = None,
) -> tuple[EmailAuthService, RecordingChallenges, RecordingEmailProvider, RecordingAuthService]:
    challenge_service = challenges or RecordingChallenges()
    email_provider = provider or RecordingEmailProvider()
    auth_service = auth or RecordingAuthService()
    service = EmailAuthService(
        db,
        challenges=challenge_service,
        email_provider=email_provider,
        auth=auth_service,
    )
    return service, challenge_service, email_provider, auth_service


def add_user(
    db: DbSession,
    email: str = "user@example.com",
    *,
    active: bool = True,
    blacklisted: bool = False,
) -> User:
    user = User(
        email=email,
        hashed_password=hash_password("old-password"),
        is_active=active,
        is_blacklisted=blacklisted,
        email_verified_at=datetime(2026, 1, 1),
    )
    db.add(user)
    db.commit()
    return user


def test_send_registration_code_sends_ses_without_exposing_code(db_session: DbSession) -> None:
    service, challenges, provider, _auth = make_service(db_session)

    response = service.send_registration_code(" User@Example.COM ", "192.0.2.10")

    assert challenges.created == [("user@example.com", "register", "192.0.2.10")]
    assert provider.sent == [("user@example.com", "123456", "register")]
    assert response.model_dump() == {
        "challenge_id": "challenge-123",
        "expires_in": 600,
        "resend_after": 60,
    }
    assert "123456" not in str(response.model_dump())


def test_registration_code_provider_failure_deletes_challenge(db_session: DbSession) -> None:
    challenges = RecordingChallenges()
    provider = RecordingEmailProvider(EmailProviderError("email provider unavailable"))
    service, _challenges, _provider, _auth = make_service(
        db_session,
        challenges=challenges,
        provider=provider,
    )

    with pytest.raises(EmailProviderError):
        service.send_registration_code("user@example.com", "192.0.2.10")

    assert challenges.deleted == ["challenge-123"]


def test_register_creates_verified_normal_user_and_auto_logs_in(db_session: DbSession) -> None:
    normal = Role(name="normal", is_enabled=True)
    db_session.add(normal)
    db_session.commit()
    service, challenges, _provider, auth = make_service(db_session)

    response = service.register(
        " New.User@Example.COM ",
        "challenge-123",
        "123456",
        "strong-password",
    )

    user = db_session.scalar(select(User).where(User.email == "new.user@example.com"))
    assert user is not None
    assert user.email_verified_at is not None
    assert user.is_active is True
    assert user.is_blacklisted is False
    assert verify_password("strong-password", user.hashed_password)
    assert db_session.scalar(
        select(user_roles.c.role_id).where(user_roles.c.user_id == user.id)
    ) == normal.id
    assert challenges.consumed == [
        ("challenge-123", "new.user@example.com", "register", "123456")
    ]
    assert auth.completed == [user.id]
    assert response.access_token == "access"


def test_register_rejects_duplicate_or_missing_normal_without_partial_user(db_session: DbSession) -> None:
    add_user(db_session)
    normal = Role(name="normal", is_enabled=True)
    db_session.add(normal)
    db_session.commit()
    service, _challenges, _provider, _auth = make_service(db_session)

    with pytest.raises(EmailAlreadyRegisteredError):
        service.register("USER@example.com", "challenge-123", "123456", "strong-password")

    normal.is_enabled = False
    db_session.commit()
    with pytest.raises(EmailAuthConfigurationError):
        service.register("new@example.com", "challenge-123", "123456", "strong-password")
    assert db_session.scalar(select(User).where(User.email == "new@example.com")) is None


def test_register_enforces_password_policy_before_consuming_challenge(db_session: DbSession) -> None:
    service, challenges, _provider, _auth = make_service(db_session)

    with pytest.raises(EmailPasswordPolicyError):
        service.register("new@example.com", "challenge-123", "123456", "short")

    assert challenges.consumed == []


def test_email_login_delegates_to_shared_auth_path(db_session: DbSession) -> None:
    service, _challenges, _provider, auth = make_service(db_session)

    response = service.login(" User@Example.COM ", "password-value")

    assert auth.logins == [("user@example.com", "password-value")]
    assert response.refresh_token == "refresh"


def test_password_reset_code_response_does_not_enumerate_users(db_session: DbSession) -> None:
    add_user(db_session)
    existing_service, existing_challenges, existing_provider, _auth = make_service(db_session)

    existing_response = existing_service.send_password_reset_code(
        "USER@example.com",
        "192.0.2.20",
    )

    missing_challenges = RecordingChallenges()
    missing_challenges.next = CreatedChallenge("challenge-missing", "654321", 600, 60)
    missing_service, _challenges, missing_provider, _auth = make_service(
        db_session,
        challenges=missing_challenges,
    )
    missing_response = missing_service.send_password_reset_code(
        "missing@example.com",
        "192.0.2.21",
    )

    assert existing_response.message == missing_response.message
    assert existing_response.expires_in == missing_response.expires_in == 600
    assert existing_response.resend_after == missing_response.resend_after == 60
    assert existing_provider.sent == [("user@example.com", "123456", "password_reset")]
    assert missing_provider.sent == []
    assert missing_challenges.deleted == ["challenge-missing"]
    assert missing_response.challenge_id == "challenge-missing"


def test_reset_password_updates_version_and_revokes_all_sessions(
    db_session: DbSession,
    fake_redis: FakeRedis,
) -> None:
    user = add_user(db_session)
    db_session.add_all(
        [
            AuthSession(sid="sid-1", user_id=user.id, status="active"),
            AuthSession(sid="sid-2", user_id=user.id, status="active"),
        ]
    )
    db_session.commit()
    challenges = RecordingChallenges()
    service = EmailAuthService(db_session, challenges=challenges)
    old_version = user.version

    response = service.reset_password(
        "USER@example.com",
        "challenge-123",
        "123456",
        "replacement-password",
    )

    db_session.refresh(user)
    assert response.message == "密码重置成功，请使用新密码登录"
    assert verify_password("replacement-password", user.hashed_password)
    assert not verify_password("old-password", user.hashed_password)
    assert user.password_changed_at is not None
    assert user.version == old_version + 1
    sessions = db_session.scalars(select(AuthSession).where(AuthSession.user_id == user.id)).all()
    assert all(item.status == "revoked" for item in sessions)
    assert all(item.revoked_reason == "password_reset" for item in sessions)
    assert set(fake_redis.values.values()) == {"revoked"}
    assert challenges.consumed == [
        ("challenge-123", "user@example.com", "password_reset", "123456")
    ]


def test_reset_password_hides_missing_or_ineligible_user(db_session: DbSession) -> None:
    add_user(db_session, active=False)
    service, challenges, _provider, _auth = make_service(db_session)

    with pytest.raises(ChallengeNotFoundError):
        service.reset_password(
            "user@example.com",
            "challenge-123",
            "123456",
            "replacement-password",
        )

    assert challenges.consumed == []
