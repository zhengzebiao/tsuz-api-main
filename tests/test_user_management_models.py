from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.audit_event import AuditEvent
from app.models.session import Session as AuthSession
from app.models.user import User


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


def test_user_management_fields_have_expected_defaults(db_session: DbSession) -> None:
    user = User(email="user@example.com", hashed_password="hashed-password")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    assert user.display_name is None
    assert user.is_active is True
    assert user.is_blacklisted is False
    assert user.disabled_at is None
    assert user.disabled_reason is None
    assert user.blacklisted_at is None
    assert user.blacklisted_reason is None
    assert user.email_verified_at is None
    assert user.password_changed_at is None
    assert isinstance(user.created_at, datetime)
    assert isinstance(user.updated_at, datetime)
    assert user.version == 1


def test_email_verification_timestamp_can_be_persisted(db_session: DbSession) -> None:
    verified_at = datetime(2026, 8, 14, 10, 30, tzinfo=UTC).replace(tzinfo=None)
    user = User(
        email="verified-user@example.com",
        hashed_password="hashed-password",
        email_verified_at=verified_at,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    assert user.email_verified_at == verified_at


def test_session_revocation_metadata_can_be_persisted(db_session: DbSession) -> None:
    user = User(email="session-user@example.com", hashed_password="hashed-password")
    db_session.add(user)
    db_session.flush()
    revoked_at = datetime(2026, 8, 11, 12, 30, tzinfo=UTC).replace(tzinfo=None)
    auth_session = AuthSession(
        sid="sid-revoked",
        user_id=user.id,
        status="revoked",
        revoked_at=revoked_at,
        revoked_reason="admin_force_logout",
    )
    db_session.add(auth_session)
    db_session.commit()
    db_session.refresh(auth_session)

    assert auth_session.revoked_at == revoked_at
    assert auth_session.revoked_reason == "admin_force_logout"


def test_audit_event_persists_changes_and_request_id(db_session: DbSession) -> None:
    actor = User(email="admin@example.com", hashed_password="hashed-password")
    target = User(email="target@example.com", hashed_password="hashed-password")
    db_session.add_all([actor, target])
    db_session.flush()
    audit_event = AuditEvent(
        actor_user_id=actor.id,
        action="user.disabled",
        target_type="user",
        target_id=target.id,
        result="success",
        reason="employment ended",
        changes_json={"is_active": {"from": True, "to": False}},
        request_id="req-user-disabled-1",
    )
    db_session.add(audit_event)
    db_session.commit()
    db_session.refresh(audit_event)

    assert audit_event.actor_user_id == actor.id
    assert audit_event.target_id == target.id
    assert audit_event.changes_json == {"is_active": {"from": True, "to": False}}
    assert audit_event.request_id == "req-user-disabled-1"
    assert isinstance(audit_event.created_at, datetime)
