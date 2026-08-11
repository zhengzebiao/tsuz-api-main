from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as DbSession, sessionmaker

import app.services.session_service as session_module
from app.core.database import Base
from app.core.security import hash_password, verify_password
from app.models.audit_event import AuditEvent
from app.models.role import Role, user_roles
from app.models.session import Session as AuthSession
from app.models.user import User
from app.schemas.admin_user import AdminUserCreate, AdminUserUpdate
from app.services.admin_user_service import (
    AdminUserService,
    EmailAlreadyExistsError,
    InvalidPasswordError,
    LastActiveAdminError,
    SelfOperationNotAllowedError,
    UserBlacklistedError,
    UserNotFoundError,
    UserVersionConflictError,
)


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


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    redis = FakeRedis()
    monkeypatch.setattr(session_module, "get_redis", lambda: redis)
    return redis


def add_user(
    db: DbSession,
    email: str,
    *,
    display_name: str | None = None,
    active: bool = True,
    blacklisted: bool = False,
) -> User:
    user = User(
        email=email,
        display_name=display_name,
        hashed_password=hash_password("password123"),
        is_active=active,
        is_blacklisted=blacklisted,
    )
    db.add(user)
    db.flush()
    return user


def add_admin_role(db: DbSession, *users: User) -> Role:
    role = db.scalar(select(Role).where(Role.name == "admin"))
    if role is None:
        role = Role(name="admin")
        db.add(role)
        db.flush()
    for user in users:
        db.execute(user_roles.insert().values(user_id=user.id, role_id=role.id))
    return role


def add_active_session(db: DbSession, user: User, sid: str) -> None:
    db.add(AuthSession(sid=sid, user_id=user.id, status="active"))


def test_list_and_get_users_support_filters_without_password_data(db_session: DbSession) -> None:
    actor = add_user(db_session, "admin@example.com", display_name="Administrator")
    add_user(db_session, "alice@example.com", display_name="Alice")
    add_user(db_session, "blocked@example.com", display_name="Blocked", blacklisted=True)
    db_session.commit()
    service = AdminUserService(db_session)

    users, total = service.list_users(page=1, page_size=10, keyword="ALI", is_blacklisted=False)

    assert total == 1
    assert [user.email for user in users] == ["alice@example.com"]
    assert service.get_user(actor.id).email == "admin@example.com"
    with pytest.raises(UserNotFoundError):
        service.get_user(999)


def test_create_user_normalizes_email_and_audits_safe_changes(db_session: DbSession) -> None:
    actor = add_user(db_session, "admin@example.com")
    db_session.commit()
    service = AdminUserService(db_session)

    user = service.create_user(
        AdminUserCreate(
            email="New.User@Example.COM",
            display_name="New User",
            password="new-password-123",
        ),
        actor_user_id=actor.id,
        request_id="req-created",
    )

    assert user.email == "new.user@example.com"
    assert user.is_blacklisted is False
    assert verify_password("new-password-123", user.hashed_password)
    audit = db_session.scalar(select(AuditEvent).where(AuditEvent.action == "user.created"))
    assert audit is not None
    assert audit.request_id == "req-created"
    assert "password" not in str(audit.changes_json)

    with pytest.raises(EmailAlreadyExistsError):
        service.create_user(
            AdminUserCreate(email="NEW.USER@example.com", password="another-password"),
            actor_user_id=actor.id,
        )
    with pytest.raises(InvalidPasswordError):
        service.create_user(
            AdminUserCreate(email="weak@example.com", password="short"),
            actor_user_id=actor.id,
        )


def test_update_user_uses_version_and_email_change_revokes_sessions(db_session: DbSession) -> None:
    actor = add_user(db_session, "admin@example.com")
    target = add_user(db_session, "target@example.com", display_name="Old")
    add_active_session(db_session, target, "sid-update")
    db_session.commit()
    service = AdminUserService(db_session)

    user, changed, revoked = service.update_user(
        target.id,
        AdminUserUpdate(email="updated@example.com", display_name=None, version=target.version),
        actor_user_id=actor.id,
        request_id="req-updated",
    )

    assert changed is True
    assert revoked == 1
    assert user.email == "updated@example.com"
    assert user.display_name is None
    assert user.version == 2
    auth_session = db_session.scalar(select(AuthSession).where(AuthSession.sid == "sid-update"))
    assert auth_session is not None
    assert auth_session.revoked_reason == "email_changed"
    audit = db_session.scalar(select(AuditEvent).where(AuditEvent.action == "user.updated"))
    assert audit is not None
    assert set(audit.changes_json or {}) == {"email", "display_name"}
    assert audit.changes_json["email"] == {
        "from": "target@example.com",
        "to": "updated@example.com",
    }

    with pytest.raises(UserVersionConflictError):
        service.update_user(
            target.id,
            AdminUserUpdate(display_name="Stale", version=1),
            actor_user_id=actor.id,
        )


def test_update_user_no_change_keeps_version(db_session: DbSession) -> None:
    actor = add_user(db_session, "admin@example.com")
    target = add_user(db_session, "target@example.com", display_name="Same")
    db_session.commit()

    user, changed, revoked = AdminUserService(db_session).update_user(
        target.id,
        AdminUserUpdate(display_name="Same", version=target.version),
        actor_user_id=actor.id,
    )

    assert changed is False
    assert revoked == 0
    assert user.version == 1


def test_disable_and_blacklist_are_idempotent_and_revoke_sessions(db_session: DbSession) -> None:
    actor = add_user(db_session, "admin@example.com")
    target = add_user(db_session, "target@example.com")
    add_active_session(db_session, target, "sid-disable")
    db_session.commit()
    service = AdminUserService(db_session)

    user, changed, revoked = service.disable_user(
        target.id,
        actor_user_id=actor.id,
        reason="left company",
    )

    assert changed is True
    assert revoked == 1
    assert user.is_active is False
    assert user.disabled_reason == "left company"
    assert user.version == 2
    first_disabled_at = user.disabled_at

    user, changed, revoked = service.disable_user(
        target.id,
        actor_user_id=actor.id,
        reason="different reason",
    )
    assert changed is False
    assert revoked == 0
    assert user.disabled_reason == "left company"
    assert user.disabled_at == first_disabled_at
    assert user.version == 2

    user, changed, _revoked = service.blacklist_user(
        target.id,
        actor_user_id=actor.id,
        reason="security risk",
    )
    assert changed is True
    assert user.is_active is False
    assert user.is_blacklisted is True
    assert user.version == 3


def test_enable_and_recover_preserve_independent_states(db_session: DbSession) -> None:
    actor = add_user(db_session, "admin@example.com")
    target = add_user(db_session, "target@example.com", active=False, blacklisted=True)
    db_session.commit()
    service = AdminUserService(db_session)

    with pytest.raises(UserBlacklistedError):
        service.enable_user(target.id, actor_user_id=actor.id)

    user, changed, _revoked = service.recover_user(target.id, actor_user_id=actor.id)
    assert changed is True
    assert user.is_blacklisted is False
    assert user.is_active is False

    user, changed, _revoked = service.enable_user(target.id, actor_user_id=actor.id)
    assert changed is True
    assert user.is_active is True
    assert user.disabled_at is None
    assert user.disabled_reason is None

    user, changed, _revoked = service.enable_user(target.id, actor_user_id=actor.id)
    assert changed is False
    assert user.version == 3


def test_self_and_last_active_admin_are_protected(db_session: DbSession) -> None:
    actor = add_user(db_session, "actor@example.com")
    other_admin = add_user(db_session, "other-admin@example.com")
    add_admin_role(db_session, actor, other_admin)
    db_session.commit()
    service = AdminUserService(db_session)

    with pytest.raises(SelfOperationNotAllowedError):
        service.disable_user(actor.id, actor_user_id=actor.id, reason="self")

    service.disable_user(other_admin.id, actor_user_id=actor.id, reason="rotation")
    with pytest.raises(LastActiveAdminError):
        service.blacklist_user(actor.id, actor_user_id=other_admin.id, reason="last admin")


def test_reset_password_and_force_logout_audit_without_secrets(db_session: DbSession) -> None:
    actor = add_user(db_session, "admin@example.com")
    target = add_user(db_session, "target@example.com")
    add_active_session(db_session, target, "sid-reset")
    db_session.commit()
    service = AdminUserService(db_session)

    revoked = service.reset_password(
        target.id,
        "replacement-password",
        actor_user_id=actor.id,
        request_id="req-reset",
    )

    assert revoked == 1
    db_session.refresh(target)
    assert verify_password("replacement-password", target.hashed_password)
    assert target.password_changed_at is not None
    assert target.version == 2
    reset_audit = db_session.scalar(select(AuditEvent).where(AuditEvent.action == "user.password_reset"))
    assert reset_audit is not None
    assert reset_audit.changes_json == {"password_changed": True, "revoked_sessions": 1}
    assert "replacement-password" not in str(reset_audit.changes_json)
    assert target.hashed_password not in str(reset_audit.changes_json)

    assert service.force_logout(
        target.id,
        actor_user_id=actor.id,
        reason="security review",
        request_id="req-force",
    ) == 0
    force_audit = db_session.scalar(select(AuditEvent).where(AuditEvent.action == "user.force_logout"))
    assert force_audit is not None
    assert force_audit.reason == "security review"
    assert force_audit.changes_json == {"revoked_sessions": 0}
