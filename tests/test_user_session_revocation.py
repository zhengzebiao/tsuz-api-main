from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

import app.services.session_service as session_module
from app.core.config import settings
from app.core.database import Base
from app.models.session import Session as AuthSession
from app.models.user import User
from app.services.session_service import SessionService


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value
        if ex is not None:
            self.expirations[key] = ex

    def get(self, key: str) -> str | None:
        return self.values.get(key)


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


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    redis = FakeRedis()
    monkeypatch.setattr(session_module, "get_redis", lambda: redis)
    return redis


def create_user_sessions(db: DbSession) -> tuple[User, User]:
    target = User(email="target@example.com", hashed_password="hashed-password")
    other = User(email="other@example.com", hashed_password="hashed-password")
    db.add_all([target, other])
    db.flush()
    db.add_all(
        [
            AuthSession(sid="target-active-1", user_id=target.id, status="active"),
            AuthSession(sid="target-active-2", user_id=target.id, status="active"),
            AuthSession(sid="target-revoked", user_id=target.id, status="revoked"),
            AuthSession(sid="other-active", user_id=other.id, status="active"),
        ]
    )
    db.commit()
    return target, other


def test_revoke_user_sessions_updates_database_and_redis(
    db_session: DbSession,
    fake_redis: FakeRedis,
) -> None:
    target, _other = create_user_sessions(db_session)
    service = SessionService(db_session)

    revoked_count = service.revoke_user_sessions(target.id, "admin_force_logout")

    assert revoked_count == 2
    db_session.expire_all()
    sessions = db_session.scalars(
        select(AuthSession).where(AuthSession.user_id == target.id).order_by(AuthSession.sid)
    ).all()
    by_sid = {session.sid: session for session in sessions}
    for sid in ("target-active-1", "target-active-2"):
        assert by_sid[sid].status == "revoked"
        assert by_sid[sid].revoked_at is not None
        assert by_sid[sid].revoked_reason == "admin_force_logout"
        key = f"{settings.session_prefix}{sid}"
        assert fake_redis.values[key] == "revoked"
        assert fake_redis.expirations[key] == settings.refresh_token_expire_days * 24 * 60 * 60
    assert by_sid["target-revoked"].revoked_reason is None
    assert f"{settings.session_prefix}target-revoked" not in fake_redis.values

    assert service.revoke_user_sessions(target.id, "admin_force_logout") == 0


def test_revoke_user_sessions_does_not_touch_other_users(
    db_session: DbSession,
    fake_redis: FakeRedis,
) -> None:
    target, other = create_user_sessions(db_session)

    SessionService(db_session).revoke_user_sessions(target.id, "user_blacklisted")

    db_session.expire_all()
    other_session = db_session.scalar(select(AuthSession).where(AuthSession.user_id == other.id))
    assert other_session is not None
    assert other_session.status == "active"
    assert f"{settings.session_prefix}other-active" not in fake_redis.values


def test_ensure_session_active_checks_database_when_redis_has_no_marker(
    db_session: DbSession,
    fake_redis: FakeRedis,
) -> None:
    target, _other = create_user_sessions(db_session)
    service = SessionService(db_session)

    service.ensure_session_active("target-active-1")

    revoked = db_session.scalar(
        select(AuthSession).where(AuthSession.user_id == target.id, AuthSession.sid == "target-revoked")
    )
    assert revoked is not None
    with pytest.raises(ValueError, match="session is revoked"):
        service.ensure_session_active(revoked.sid)
    assert fake_redis.values == {}
