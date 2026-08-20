from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

import app.services.session_service as session_module
from app.core.database import Base
from app.models.audit_event import AuditEvent
from app.models.permission import Permission
from app.models.role import Role, role_permissions, user_roles
from app.models.session import Session as AuthSession
from app.models.user import User
from app.schemas.admin_role import AdminRoleCreate, AdminRoleUpdate
from app.services.admin_permission_service import (
    PermissionDisabledError,
    PermissionNotDeclaredError,
    PermissionNotFoundError,
)
from app.services.admin_role_service import (
    AdminRoleService,
    ProtectedRoleOperationError,
    RoleNameAlreadyExistsError,
    RoleNotFoundError,
    RoleVersionConflictError,
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


def add_permission(
    db: DbSession,
    name: str,
    *,
    declared: bool = True,
    enabled: bool = True,
) -> Permission:
    permission = Permission(
        name=name,
        display_name=name,
        is_declared=declared,
        is_enabled=enabled,
    )
    db.add(permission)
    db.flush()
    return permission


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
        hashed_password="not-used-by-role-service",
        is_active=active,
        is_blacklisted=blacklisted,
    )
    db.add(user)
    db.flush()
    return user


def add_role(
    db: DbSession,
    name: str,
    *,
    description: str = "",
    enabled: bool = True,
    created_at: datetime | None = None,
) -> Role:
    role = Role(name=name, description=description, is_enabled=enabled)
    if created_at is not None:
        role.created_at = created_at
        role.updated_at = created_at
    db.add(role)
    db.flush()
    return role


def attach_role(db: DbSession, user: User, role: Role) -> None:
    db.execute(user_roles.insert().values(user_id=user.id, role_id=role.id))


def add_active_session(db: DbSession, user: User, sid: str) -> None:
    db.add(AuthSession(sid=sid, user_id=user.id, status="active"))


def role_audits(db: DbSession) -> list[AuditEvent]:
    return list(
        db.scalars(
            select(AuditEvent)
            .where(AuditEvent.target_type == "role")
            .order_by(AuditEvent.id)
        )
    )


def test_list_and_get_roles_support_filters_pagination_and_stable_order(db_session: DbSession) -> None:
    created_at = datetime(2026, 8, 13, 10, 30, tzinfo=UTC).replace(tzinfo=None)
    first = add_role(
        db_session,
        "auditor",
        description="Project audit access",
        created_at=created_at,
    )
    second = add_role(
        db_session,
        "operator",
        description="Project operations",
        created_at=created_at,
    )
    disabled = add_role(
        db_session,
        "archive",
        description="Historical records",
        enabled=False,
        created_at=datetime(2026, 8, 12, 10, 30, tzinfo=UTC).replace(tzinfo=None),
    )
    db_session.commit()
    service = AdminRoleService(db_session)

    roles, total = service.list_roles(
        page=1,
        page_size=1,
        keyword="PROJECT",
        is_enabled=True,
    )

    assert total == 2
    assert [role.id for role in roles] == [second.id]
    assert service.list_roles(page=2, page_size=1, keyword="project", is_enabled=True)[0][0].id == first.id
    assert service.list_roles(keyword="HISTORICAL")[0][0].id == disabled.id
    assert service.get_role(first.id).name == "auditor"
    with pytest.raises(RoleNotFoundError, match="ROLE_NOT_FOUND"):
        service.get_role(999)


def test_create_role_is_enabled_and_adds_safe_audit(db_session: DbSession) -> None:
    actor = add_user(db_session, "admin@example.com")
    db_session.commit()

    role = AdminRoleService(db_session).create_role(
        AdminRoleCreate(name="auditor", description="Audit access"),
        actor_user_id=actor.id,
        request_id="req-role-create",
    )

    assert role.id is not None
    assert role.is_enabled is True
    assert role.version == 1
    audit = role_audits(db_session)[0]
    assert audit.action == "role.created"
    assert audit.target_id == role.id
    assert audit.request_id == "req-role-create"
    assert audit.changes_json == {
        "name": {"from": None, "to": "auditor"},
        "description": {"from": None, "to": "Audit access"},
        "is_enabled": {"from": None, "to": True},
    }
    audit_text = str(audit.changes_json).lower()
    assert "password" not in audit_text
    assert "token" not in audit_text
    assert "session" not in audit_text
    assert "permission" not in audit_text


def test_create_and_update_reject_duplicate_role_names(db_session: DbSession) -> None:
    actor = add_user(db_session, "admin@example.com")
    existing = add_role(db_session, "auditor")
    target = add_role(db_session, "operator")
    db_session.commit()
    service = AdminRoleService(db_session)

    with pytest.raises(RoleNameAlreadyExistsError, match="ROLE_NAME_ALREADY_EXISTS"):
        service.create_role(AdminRoleCreate(name=existing.name), actor_user_id=actor.id)

    with pytest.raises(RoleNameAlreadyExistsError, match="ROLE_NAME_ALREADY_EXISTS"):
        service.update_role(
            target.id,
            AdminRoleUpdate(name=existing.name, version=target.version),
            actor_user_id=actor.id,
        )

    db_session.refresh(target)
    assert target.name == "operator"
    assert target.version == 1
    assert role_audits(db_session) == []


def test_create_role_maps_concurrent_unique_conflict_without_rolling_back_caller_transaction(
    db_session: DbSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = add_user(db_session, "admin@example.com")
    existing = add_role(db_session, "auditor")
    actor_id = actor.id
    db_session.commit()
    original_flush = db_session.flush
    original_scalar = db_session.scalar
    failed = False
    scalar_calls = 0

    def flush_with_conflict(*args: Any, **kwargs: Any) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise IntegrityError("INSERT INTO roles", {}, Exception("unique conflict"))
        original_flush(*args, **kwargs)

    def scalar_with_conflict(statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal scalar_calls
        scalar_calls += 1
        if scalar_calls == 1:
            return None
        return existing.id

    monkeypatch.setattr(db_session, "flush", flush_with_conflict)
    monkeypatch.setattr(db_session, "scalar", scalar_with_conflict)

    with pytest.raises(RoleNameAlreadyExistsError, match="ROLE_NAME_ALREADY_EXISTS"):
        AdminRoleService(db_session).create_role(
            AdminRoleCreate(name="auditor"),
            actor_user_id=actor_id,
        )

    monkeypatch.setattr(db_session, "flush", original_flush)
    monkeypatch.setattr(db_session, "scalar", original_scalar)
    assert db_session.get(User, actor_id) is not None
    assert db_session.scalar(select(func.count()).select_from(Role)) == 1
    assert role_audits(db_session) == []


def test_update_role_uses_version_and_skips_no_change(db_session: DbSession) -> None:
    actor = add_user(db_session, "admin@example.com")
    role = add_role(db_session, "auditor", description="Old description")
    db_session.commit()
    service = AdminRoleService(db_session)

    updated, changed, revoked = service.update_role(
        role.id,
        AdminRoleUpdate(description="New description", version=role.version),
        actor_user_id=actor.id,
        request_id="req-role-update",
    )

    assert changed is True
    assert revoked == 0
    assert updated.description == "New description"
    assert updated.version == 2
    audit = role_audits(db_session)[0]
    assert audit.action == "role.updated"
    assert audit.request_id == "req-role-update"
    assert audit.changes_json == {
        "description": {"from": "Old description", "to": "New description"},
        "revoked_sessions": 0,
    }

    unchanged, changed, revoked = service.update_role(
        role.id,
        AdminRoleUpdate(description="New description", version=updated.version),
        actor_user_id=actor.id,
    )
    assert changed is False
    assert revoked == 0
    assert unchanged.version == 2
    assert len(role_audits(db_session)) == 1

    with pytest.raises(RoleVersionConflictError, match="ROLE_VERSION_CONFLICT"):
        service.update_role(
            role.id,
            AdminRoleUpdate(description="Stale", version=1),
            actor_user_id=actor.id,
        )
    assert len(role_audits(db_session)) == 1


def test_role_name_change_revokes_all_associated_user_sessions(
    db_session: DbSession,
    fake_redis: FakeRedis,
) -> None:
    actor = add_user(db_session, "admin@example.com")
    first_user = add_user(db_session, "first@example.com")
    second_user = add_user(db_session, "second@example.com")
    role = add_role(db_session, "auditor")
    attach_role(db_session, first_user, role)
    attach_role(db_session, second_user, role)
    add_active_session(db_session, first_user, "sid-first")
    add_active_session(db_session, second_user, "sid-second")
    db_session.commit()

    updated, changed, revoked = AdminRoleService(db_session).update_role(
        role.id,
        AdminRoleUpdate(name="reviewer", version=role.version),
        actor_user_id=actor.id,
    )

    assert changed is True
    assert revoked == 2
    assert updated.name == "reviewer"
    sessions = db_session.scalars(select(AuthSession).order_by(AuthSession.id)).all()
    assert [session.status for session in sessions] == ["revoked", "revoked"]
    assert {session.revoked_reason for session in sessions} == {"role_name_changed"}
    assert set(fake_redis.values.values()) == {"revoked"}
    assert role_audits(db_session)[0].changes_json["revoked_sessions"] == 2


def test_admin_role_cannot_be_renamed_or_disabled(db_session: DbSession) -> None:
    actor = add_user(db_session, "admin@example.com")
    role = add_role(db_session, "admin", description="Core administration")
    db_session.commit()
    service = AdminRoleService(db_session)

    with pytest.raises(ProtectedRoleOperationError, match="PROTECTED_ROLE_OPERATION"):
        service.update_role(
            role.id,
            AdminRoleUpdate(name="superadmin", version=role.version),
            actor_user_id=actor.id,
        )
    with pytest.raises(ProtectedRoleOperationError, match="PROTECTED_ROLE_OPERATION"):
        service.disable_role(role.id, actor_user_id=actor.id, reason="forbidden")

    updated, changed, revoked = service.update_role(
        role.id,
        AdminRoleUpdate(description="Updated core administration", version=role.version),
        actor_user_id=actor.id,
    )
    assert changed is True
    assert revoked == 0
    assert updated.name == "admin"
    assert updated.description == "Updated core administration"


def test_disable_and_enable_are_idempotent_preserve_associations_and_audit(
    db_session: DbSession,
) -> None:
    actor = add_user(db_session, "admin@example.com")
    target = add_user(db_session, "target@example.com")
    role = add_role(db_session, "auditor")
    permission = Permission(name="audit:read", description="Read audit data")
    db_session.add(permission)
    db_session.flush()
    attach_role(db_session, target, role)
    db_session.execute(role_permissions.insert().values(role_id=role.id, permission_id=permission.id))
    add_active_session(db_session, target, "sid-disable-role")
    db_session.commit()
    service = AdminRoleService(db_session)

    disabled, changed, revoked = service.disable_role(
        role.id,
        actor_user_id=actor.id,
        reason="maintenance",
        request_id="req-role-disable",
    )

    assert changed is True
    assert revoked == 1
    assert disabled.is_enabled is False
    assert disabled.disabled_at is not None
    assert disabled.disabled_reason == "maintenance"
    assert disabled.version == 2
    first_disabled_at = disabled.disabled_at

    disabled, changed, revoked = service.disable_role(
        role.id,
        actor_user_id=actor.id,
        reason="must not replace original reason",
    )
    assert changed is False
    assert revoked == 0
    assert disabled.disabled_at == first_disabled_at
    assert disabled.disabled_reason == "maintenance"
    assert disabled.version == 2

    enabled, changed, revoked = service.enable_role(
        role.id,
        actor_user_id=actor.id,
        request_id="req-role-enable",
    )
    assert changed is True
    assert revoked == 0
    assert enabled.is_enabled is True
    assert enabled.disabled_at is None
    assert enabled.disabled_reason is None
    assert enabled.version == 3

    enabled, changed, revoked = service.enable_role(role.id, actor_user_id=actor.id)
    assert changed is False
    assert revoked == 0
    assert enabled.version == 3
    assert db_session.scalar(select(func.count()).select_from(user_roles)) == 1
    assert db_session.scalar(select(func.count()).select_from(role_permissions)) == 1
    audits = role_audits(db_session)
    assert [audit.action for audit in audits] == ["role.disabled", "role.enabled"]
    assert audits[0].reason == "maintenance"
    assert audits[0].changes_json["revoked_sessions"] == 1
    assert audits[1].changes_json["revoked_sessions"] == 0


def test_role_permission_assignment_replaces_full_set_idempotently_and_audits(
    db_session: DbSession,
    fake_redis: FakeRedis,
) -> None:
    actor = add_user(db_session, "actor@example.com")
    first_user = add_user(db_session, "first@example.com")
    second_user = add_user(db_session, "second@example.com")
    role = add_role(db_session, "auditor")
    app_read = add_permission(db_session, "app:read")
    role_read = add_permission(db_session, "role:read")
    user_read = add_permission(db_session, "user:read")
    attach_role(db_session, first_user, role)
    attach_role(db_session, second_user, role)
    db_session.execute(
        role_permissions.insert().values(
            role_id=role.id,
            permission_id=app_read.id,
        )
    )
    add_active_session(db_session, first_user, "sid-permission-first")
    add_active_session(db_session, second_user, "sid-permission-second")
    db_session.commit()

    service = AdminRoleService(db_session)
    assert [
        permission.name for permission in service.get_role_permissions(role.id)
    ] == ["app:read"]

    assigned_role, permissions, changed, revoked = service.assign_permissions(
        role.id,
        [user_read.id, role_read.id],
        role.version,
        actor_user_id=actor.id,
        request_id="req-role-permissions",
    )

    assert changed is True
    assert revoked == 2
    assert assigned_role.version == 2
    assert [permission.name for permission in permissions] == [
        "role:read",
        "user:read",
    ]
    assert set(
        db_session.scalars(
            select(role_permissions.c.permission_id).where(
                role_permissions.c.role_id == role.id
            )
        ).all()
    ) == {role_read.id, user_read.id}
    assert set(fake_redis.values.values()) == {"revoked"}
    audit = role_audits(db_session)[0]
    assert audit.action == "role.permissions_assigned"
    assert audit.request_id == "req-role-permissions"
    assert audit.changes_json == {
        "permissions": {
            "from": [{"id": app_read.id, "name": "app:read"}],
            "to": [
                {"id": role_read.id, "name": "role:read"},
                {"id": user_read.id, "name": "user:read"},
            ],
        },
        "revoked_sessions": 2,
    }
    audit_text = str(audit.changes_json).lower()
    assert "password" not in audit_text
    assert "token" not in audit_text
    assert "sid-permission" not in audit_text

    unchanged_role, unchanged_permissions, changed, revoked = (
        service.assign_permissions(
            role.id,
            [role_read.id, user_read.id],
            assigned_role.version,
            actor_user_id=actor.id,
        )
    )
    assert changed is False
    assert revoked == 0
    assert unchanged_role.version == 2
    assert [permission.name for permission in unchanged_permissions] == [
        "role:read",
        "user:read",
    ]
    assert len(role_audits(db_session)) == 1

    cleared, permissions, changed, revoked = service.assign_permissions(
        role.id,
        [],
        unchanged_role.version,
        actor_user_id=actor.id,
    )
    assert changed is True
    assert revoked == 0
    assert cleared.version == 3
    assert permissions == []
    assert db_session.scalar(
        select(func.count())
        .select_from(role_permissions)
        .where(role_permissions.c.role_id == role.id)
    ) == 0


def test_role_permission_assignment_validates_targets_status_version_and_admin(
    db_session: DbSession,
) -> None:
    actor = add_user(db_session, "actor@example.com")
    role = add_role(db_session, "auditor")
    admin = add_role(db_session, "admin")
    active = add_permission(db_session, "app:read")
    disabled = add_permission(db_session, "app:update", enabled=False)
    missing = add_permission(db_session, "legacy:read", declared=False)
    db_session.execute(
        role_permissions.insert().values(
            role_id=admin.id,
            permission_id=active.id,
        )
    )
    db_session.commit()
    service = AdminRoleService(db_session)

    with pytest.raises(PermissionNotFoundError, match="PERMISSION_NOT_FOUND"):
        service.assign_permissions(
            role.id,
            [999],
            role.version,
            actor_user_id=actor.id,
        )
    with pytest.raises(PermissionDisabledError, match="PERMISSION_DISABLED"):
        service.assign_permissions(
            role.id,
            [disabled.id],
            role.version,
            actor_user_id=actor.id,
        )
    with pytest.raises(PermissionNotDeclaredError, match="PERMISSION_NOT_DECLARED"):
        service.assign_permissions(
            role.id,
            [missing.id],
            role.version,
            actor_user_id=actor.id,
        )
    with pytest.raises(RoleVersionConflictError, match="ROLE_VERSION_CONFLICT"):
        service.assign_permissions(
            role.id,
            [active.id],
            999,
            actor_user_id=actor.id,
        )
    with pytest.raises(ProtectedRoleOperationError, match="PROTECTED_ROLE_OPERATION"):
        service.assign_permissions(
            admin.id,
            [],
            admin.version,
            actor_user_id=actor.id,
        )

    db_session.expire_all()
    assert service.get_role_permissions(role.id) == []
    assert [permission.id for permission in service.get_role_permissions(admin.id)] == [
        active.id
    ]
    assert role_audits(db_session) == []


def test_existing_invalid_role_permissions_can_be_retained_or_removed(
    db_session: DbSession,
) -> None:
    actor = add_user(db_session, "actor@example.com")
    role = add_role(db_session, "auditor")
    disabled = add_permission(db_session, "app:update", enabled=False)
    missing = add_permission(db_session, "legacy:read", declared=False)
    db_session.execute(
        role_permissions.insert(),
        [
            {"role_id": role.id, "permission_id": disabled.id},
            {"role_id": role.id, "permission_id": missing.id},
        ],
    )
    db_session.commit()
    service = AdminRoleService(db_session)

    unchanged, permissions, changed, revoked = service.assign_permissions(
        role.id,
        [disabled.id, missing.id],
        role.version,
        actor_user_id=actor.id,
    )
    assert changed is False
    assert revoked == 0
    assert unchanged.version == 1
    assert [permission.name for permission in permissions] == [
        "app:update",
        "legacy:read",
    ]

    updated, permissions, changed, revoked = service.assign_permissions(
        role.id,
        [missing.id],
        unchanged.version,
        actor_user_id=actor.id,
    )
    assert changed is True
    assert revoked == 0
    assert updated.version == 2
    assert [permission.name for permission in permissions] == ["legacy:read"]


def test_role_permission_assignment_shares_caller_transaction(
    db_session: DbSession,
) -> None:
    actor = add_user(db_session, "actor@example.com")
    target = add_user(db_session, "target@example.com")
    role = add_role(db_session, "auditor")
    permission = add_permission(db_session, "app:read")
    attach_role(db_session, target, role)
    add_active_session(db_session, target, "sid-rollback-permissions")
    role_id = role.id
    permission_id = permission.id
    db_session.commit()

    changed_role, permissions, changed, revoked = (
        AdminRoleService(db_session).assign_permissions(
            role_id,
            [permission_id],
            role.version,
            actor_user_id=actor.id,
        )
    )
    assert changed is True
    assert revoked == 1
    assert [item.id for item in permissions] == [permission_id]
    assert changed_role.version == 2
    assert len(role_audits(db_session)) == 1

    db_session.rollback()

    restored = db_session.get(Role, role_id)
    session = db_session.scalar(
        select(AuthSession).where(
            AuthSession.sid == "sid-rollback-permissions"
        )
    )
    assert restored is not None
    assert restored.version == 1
    assert session is not None
    assert session.status == "active"
    assert AdminRoleService(db_session).get_role_permissions(role_id) == []
    assert role_audits(db_session) == []


def test_list_role_users_supports_disabled_roles_filters_and_empty_results(db_session: DbSession) -> None:
    actor = add_user(db_session, "actor@example.com")
    alice = add_user(db_session, "alice@example.com", display_name="Alice Reviewer")
    blocked = add_user(
        db_session,
        "blocked@example.com",
        display_name="Blocked Reviewer",
        blacklisted=True,
    )
    role = add_role(db_session, "auditor", enabled=False)
    empty_role = add_role(db_session, "operator")
    attach_role(db_session, alice, role)
    attach_role(db_session, blocked, role)
    db_session.commit()
    service = AdminRoleService(db_session)

    users, total = service.list_role_users(
        role.id,
        page=1,
        page_size=10,
        keyword="REVIEWER",
        is_active=True,
        is_blacklisted=False,
    )

    assert total == 1
    assert [user.id for user in users] == [alice.id]
    assert service.list_role_users(empty_role.id) == ([], 0)
    assert service.get_role(role.id).is_enabled is False
    with pytest.raises(RoleNotFoundError, match="ROLE_NOT_FOUND"):
        service.list_role_users(999)
    assert actor.hashed_password not in str([user.email for user in users])


def test_status_operations_issue_for_update(db_session: DbSession, monkeypatch: pytest.MonkeyPatch) -> None:
    actor = add_user(db_session, "admin@example.com")
    role = add_role(db_session, "auditor")
    db_session.commit()
    original_scalar = db_session.scalar
    statements: list[Any] = []

    def scalar_spy(statement: Any, *args: Any, **kwargs: Any) -> Any:
        statements.append(statement)
        return original_scalar(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "scalar", scalar_spy)
    service = AdminRoleService(db_session)

    service.disable_role(role.id, actor_user_id=actor.id)
    service.enable_role(role.id, actor_user_id=actor.id)

    lock_statements = [
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in statements
        if getattr(statement, "_for_update_arg", None) is not None
    ]
    assert len(lock_statements) == 2
    assert all("FOR UPDATE" in statement for statement in lock_statements)


def test_role_permission_assignment_locks_role_and_target_permissions(
    db_session: DbSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = add_user(db_session, "actor-permissions@example.com")
    role = add_role(db_session, "permission-manager")
    permission = add_permission(db_session, "app:read")
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

    AdminRoleService(db_session).assign_permissions(
        role.id,
        [permission.id],
        role.version,
        actor_user_id=actor.id,
    )

    lock_statements = [
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in statements
        if getattr(statement, "_for_update_arg", None) is not None
    ]
    assert len(lock_statements) == 2
    assert any("FROM roles" in statement for statement in lock_statements)
    assert any("FROM permissions" in statement for statement in lock_statements)


def test_role_change_and_audit_share_caller_transaction(db_session: DbSession) -> None:
    actor = add_user(db_session, "admin@example.com")
    target = add_user(db_session, "target@example.com")
    role = add_role(db_session, "auditor")
    attach_role(db_session, target, role)
    add_active_session(db_session, target, "sid-rollback-role")
    role_id = role.id
    db_session.commit()

    changed_role, changed, revoked = AdminRoleService(db_session).disable_role(
        role_id,
        actor_user_id=actor.id,
        reason="rollback test",
    )
    assert changed is True
    assert revoked == 1
    assert changed_role.is_enabled is False
    assert len(role_audits(db_session)) == 1

    db_session.rollback()

    restored = db_session.get(Role, role_id)
    session = db_session.scalar(select(AuthSession).where(AuthSession.sid == "sid-rollback-role"))
    assert restored is not None
    assert restored.is_enabled is True
    assert restored.disabled_at is None
    assert restored.disabled_reason is None
    assert restored.version == 1
    assert session is not None
    assert session.status == "active"
    assert session.revoked_at is None
    assert role_audits(db_session) == []
