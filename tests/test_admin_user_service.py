from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

import app.services.session_service as session_module
from app.core.database import Base
from app.core.security import hash_password, verify_password
from app.models.audit_event import AuditEvent
from app.models.role import Role, user_roles
from app.models.session import Session as AuthSession
from app.models.user import User
from app.schemas.admin_user import AdminUserCreate, AdminUserUpdate
from app.services.admin_role_service import RoleDisabledError, RoleNotFoundError
from app.services.admin_user_service import (
    AdminUserService,
    EmailAlreadyExistsError,
    InvalidPasswordError,
    LastActiveAdminError,
    PasswordResetUnavailableError,
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


def add_role(
    db: DbSession,
    name: str,
    *,
    enabled: bool = True,
) -> Role:
    role = Role(name=name, is_enabled=enabled)
    db.add(role)
    db.flush()
    return role


def attach_role(db: DbSession, user: User, role: Role) -> None:
    db.execute(user_roles.insert().values(user_id=user.id, role_id=role.id))


def add_active_session(db: DbSession, user: User, sid: str) -> None:
    db.add(AuthSession(sid=sid, user_id=user.id, status="active"))


def assigned_role_ids(db: DbSession, user_id: int) -> set[int]:
    return set(db.scalars(select(user_roles.c.role_id).where(user_roles.c.user_id == user_id)).all())


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


def test_reset_password_rejects_qq_only_user_without_mutation(db_session: DbSession) -> None:
    actor = add_user(db_session, "admin@example.com")
    target = User(email=None, hashed_password=None, is_active=True)
    db_session.add(target)
    db_session.flush()
    add_active_session(db_session, target, "sid-qq-only-reset")
    db_session.commit()
    original_version = target.version
    service = AdminUserService(db_session)

    with pytest.raises(PasswordResetUnavailableError, match="PASSWORD_RESET_UNAVAILABLE"):
        service.reset_password(
            target.id,
            "replacement-password",
            actor_user_id=actor.id,
            request_id="req-qq-only-reset",
        )

    db_session.refresh(target)
    assert target.hashed_password is None
    assert target.version == original_version
    auth_session = db_session.scalar(
        select(AuthSession).where(AuthSession.sid == "sid-qq-only-reset")
    )
    assert auth_session is not None
    assert auth_session.status == "active"
    assert auth_session.revoked_at is None
    assert db_session.scalar(select(AuditEvent).where(AuditEvent.action == "user.password_reset")) is None


def test_get_user_roles_returns_all_roles_in_stable_order(db_session: DbSession) -> None:
    target = add_user(db_session, "target@example.com")
    operator = add_role(db_session, "operator")
    disabled_auditor = add_role(db_session, "auditor", enabled=False)
    attach_role(db_session, target, operator)
    attach_role(db_session, target, disabled_auditor)
    db_session.commit()
    service = AdminUserService(db_session)

    roles = service.get_user_roles(target.id)

    assert [role.name for role in roles] == ["auditor", "operator"]
    assert roles[0].is_enabled is False
    with pytest.raises(UserNotFoundError, match="USER_NOT_FOUND"):
        service.get_user_roles(999)


def test_assign_roles_replaces_set_updates_version_revokes_sessions_and_audits(
    db_session: DbSession,
    fake_redis: FakeRedis,
) -> None:
    actor = add_user(db_session, "admin@example.com")
    target = add_user(db_session, "target@example.com")
    auditor = add_role(db_session, "auditor")
    operator = add_role(db_session, "operator")
    reviewer = add_role(db_session, "reviewer")
    attach_role(db_session, target, auditor)
    attach_role(db_session, target, operator)
    add_active_session(db_session, target, "sid-role-assignment")
    db_session.commit()

    user, roles, changed, revoked = AdminUserService(db_session).assign_roles(
        target.id,
        [operator.id, reviewer.id],
        target.version,
        actor_user_id=actor.id,
        request_id="req-role-assignment",
    )

    assert changed is True
    assert revoked == 1
    assert user.version == 2
    assert [role.name for role in roles] == ["operator", "reviewer"]
    assert assigned_role_ids(db_session, target.id) == {operator.id, reviewer.id}
    auth_session = db_session.scalar(select(AuthSession).where(AuthSession.sid == "sid-role-assignment"))
    assert auth_session is not None
    assert auth_session.status == "revoked"
    assert auth_session.revoked_reason == "user_roles_changed"
    assert set(fake_redis.values.values()) == {"revoked"}
    audit = db_session.scalar(select(AuditEvent).where(AuditEvent.action == "user.roles_assigned"))
    assert audit is not None
    assert audit.request_id == "req-role-assignment"
    assert audit.changes_json == {
        "roles": {
            "from": [
                {"id": auditor.id, "name": "auditor"},
                {"id": operator.id, "name": "operator"},
            ],
            "to": [
                {"id": operator.id, "name": "operator"},
                {"id": reviewer.id, "name": "reviewer"},
            ],
        },
        "revoked_sessions": 1,
    }
    audit_text = str(audit.changes_json).lower()
    assert "password" not in audit_text
    assert "permission" not in audit_text
    assert "sid-role-assignment" not in audit_text


def test_assign_roles_allows_clearing_normal_user_and_is_idempotent(db_session: DbSession) -> None:
    actor = add_user(db_session, "admin@example.com")
    target = add_user(db_session, "target@example.com")
    auditor = add_role(db_session, "auditor")
    attach_role(db_session, target, auditor)
    db_session.commit()
    service = AdminUserService(db_session)

    user, roles, changed, revoked = service.assign_roles(
        target.id,
        [],
        target.version,
        actor_user_id=actor.id,
    )

    assert changed is True
    assert revoked == 0
    assert roles == []
    assert user.version == 2
    assert assigned_role_ids(db_session, target.id) == set()

    audit_count = db_session.scalar(select(func.count()).select_from(AuditEvent))
    unchanged, roles, changed, revoked = service.assign_roles(
        target.id,
        [],
        user.version,
        actor_user_id=actor.id,
    )
    assert changed is False
    assert revoked == 0
    assert roles == []
    assert unchanged.version == 2
    assert db_session.scalar(select(func.count()).select_from(AuditEvent)) == audit_count


def test_assign_roles_rejects_invalid_or_disabled_roles_without_partial_update(db_session: DbSession) -> None:
    actor = add_user(db_session, "admin@example.com")
    target = add_user(db_session, "target@example.com")
    current = add_role(db_session, "current")
    enabled = add_role(db_session, "enabled")
    disabled = add_role(db_session, "disabled", enabled=False)
    attach_role(db_session, target, current)
    db_session.commit()
    service = AdminUserService(db_session)

    with pytest.raises(RoleNotFoundError, match="ROLE_NOT_FOUND"):
        service.assign_roles(
            target.id,
            [enabled.id, 999],
            target.version,
            actor_user_id=actor.id,
        )
    assert assigned_role_ids(db_session, target.id) == {current.id}
    assert target.version == 1

    with pytest.raises(RoleDisabledError, match="ROLE_DISABLED"):
        service.assign_roles(
            target.id,
            [current.id, disabled.id],
            target.version,
            actor_user_id=actor.id,
        )
    assert assigned_role_ids(db_session, target.id) == {current.id}
    assert target.version == 1
    assert db_session.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_assign_roles_can_preserve_existing_disabled_role(db_session: DbSession) -> None:
    actor = add_user(db_session, "admin@example.com")
    target = add_user(db_session, "target@example.com")
    disabled = add_role(db_session, "legacy", enabled=False)
    enabled = add_role(db_session, "operator")
    attach_role(db_session, target, disabled)
    db_session.commit()

    user, roles, changed, revoked = AdminUserService(db_session).assign_roles(
        target.id,
        [disabled.id, enabled.id],
        target.version,
        actor_user_id=actor.id,
    )

    assert changed is True
    assert revoked == 0
    assert user.version == 2
    assert {role.id for role in roles} == {disabled.id, enabled.id}
    assert assigned_role_ids(db_session, target.id) == {disabled.id, enabled.id}


def test_assign_roles_rejects_stale_version_before_changes(db_session: DbSession) -> None:
    actor = add_user(db_session, "admin@example.com")
    target = add_user(db_session, "target@example.com")
    role = add_role(db_session, "auditor")
    db_session.commit()

    with pytest.raises(UserVersionConflictError, match="USER_VERSION_CONFLICT"):
        AdminUserService(db_session).assign_roles(
            target.id,
            [role.id],
            target.version + 1,
            actor_user_id=actor.id,
        )

    assert assigned_role_ids(db_session, target.id) == set()
    assert target.version == 1
    assert db_session.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_assign_roles_protects_self_and_last_active_admin(db_session: DbSession) -> None:
    actor = add_user(db_session, "actor@example.com")
    other_admin = add_user(db_session, "other-admin@example.com")
    admin_role = add_admin_role(db_session, actor, other_admin)
    db_session.commit()
    service = AdminUserService(db_session)

    with pytest.raises(SelfOperationNotAllowedError, match="SELF_OPERATION_NOT_ALLOWED"):
        service.assign_roles(
            actor.id,
            [],
            actor.version,
            actor_user_id=actor.id,
        )
    assert assigned_role_ids(db_session, actor.id) == {admin_role.id}

    user, _roles, changed, _revoked = service.assign_roles(
        other_admin.id,
        [],
        other_admin.version,
        actor_user_id=actor.id,
    )
    assert changed is True
    assert user.version == 2
    assert assigned_role_ids(db_session, other_admin.id) == set()

    with pytest.raises(LastActiveAdminError, match="LAST_ACTIVE_ADMIN"):
        service.assign_roles(
            actor.id,
            [],
            actor.version,
            actor_user_id=other_admin.id,
        )
    assert assigned_role_ids(db_session, actor.id) == {admin_role.id}


def test_assign_roles_locks_user_and_target_roles(
    db_session: DbSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = add_user(db_session, "actor@example.com")
    target = add_user(db_session, "target@example.com")
    role = add_role(db_session, "auditor")
    db_session.commit()
    original_scalar = db_session.scalar
    original_scalars = db_session.scalars
    statements: list[Any] = []

    def scalar_spy(statement: Any, *args: Any, **kwargs: Any) -> Any:
        statements.append(statement)
        return original_scalar(statement, *args, **kwargs)

    def scalars_spy(statement: Any, *args: Any, **kwargs: Any) -> Any:
        statements.append(statement)
        return original_scalars(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "scalar", scalar_spy)
    monkeypatch.setattr(db_session, "scalars", scalars_spy)

    AdminUserService(db_session).assign_roles(
        target.id,
        [role.id],
        target.version,
        actor_user_id=actor.id,
    )

    lock_statements = [
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in statements
        if getattr(statement, "_for_update_arg", None) is not None
    ]
    assert len(lock_statements) == 3
    assert all("FOR UPDATE" in statement for statement in lock_statements)
    assert any("FROM users" in statement for statement in lock_statements)
    assert any("FROM roles" in statement for statement in lock_statements)
    assert any("FROM sessions" in statement for statement in lock_statements)


def test_assign_roles_locks_user_and_admin_role(
    db_session: DbSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = add_user(db_session, "actor@example.com")
    target = add_user(db_session, "target@example.com")
    add_admin_role(db_session, actor, target)
    db_session.commit()
    original_scalar = db_session.scalar
    statements: list[Any] = []

    def scalar_spy(statement: Any, *args: Any, **kwargs: Any) -> Any:
        statements.append(statement)
        return original_scalar(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "scalar", scalar_spy)

    AdminUserService(db_session).assign_roles(
        target.id,
        [],
        target.version,
        actor_user_id=actor.id,
    )

    lock_statements = [
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in statements
        if getattr(statement, "_for_update_arg", None) is not None
    ]
    assert len(lock_statements) == 2
    assert all("FOR UPDATE" in statement for statement in lock_statements)


def test_role_assignment_and_audit_share_caller_transaction(db_session: DbSession) -> None:
    actor = add_user(db_session, "admin@example.com")
    target = add_user(db_session, "target@example.com")
    old_role = add_role(db_session, "auditor")
    new_role = add_role(db_session, "operator")
    attach_role(db_session, target, old_role)
    add_active_session(db_session, target, "sid-rollback-assignment")
    target_id = target.id
    db_session.commit()

    user, roles, changed, revoked = AdminUserService(db_session).assign_roles(
        target_id,
        [new_role.id],
        target.version,
        actor_user_id=actor.id,
    )
    assert changed is True
    assert revoked == 1
    assert user.version == 2
    assert [role.id for role in roles] == [new_role.id]
    assert assigned_role_ids(db_session, target_id) == {new_role.id}
    assert db_session.scalar(select(func.count()).select_from(AuditEvent)) == 1

    db_session.rollback()

    restored = db_session.get(User, target_id)
    session = db_session.scalar(select(AuthSession).where(AuthSession.sid == "sid-rollback-assignment"))
    assert restored is not None
    assert restored.version == 1
    assert assigned_role_ids(db_session, target_id) == {old_role.id}
    assert session is not None
    assert session.status == "active"
    assert session.revoked_at is None
    assert db_session.scalar(select(func.count()).select_from(AuditEvent)) == 0
