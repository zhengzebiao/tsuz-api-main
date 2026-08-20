from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

import app.services.session_service as session_module
from app.core.database import Base
from app.models.audit_event import AuditEvent
from app.models.permission import Permission
from app.models.permission_endpoint import PermissionEndpoint
from app.models.role import Role, role_permissions, user_roles
from app.models.session import Session as AuthSession
from app.models.user import User
from app.schemas.admin_permission import AdminPermissionUpdate
from app.services.admin_permission_service import (
    AdminPermissionService,
    PermissionNotDeclaredError,
    PermissionNotFoundError,
    PermissionVersionConflictError,
    ProtectedPermissionOperationError,
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


class RecordingSessionService:
    def __init__(self, counts: dict[int, int] | None = None) -> None:
        self.counts = counts or {}
        self.calls: list[tuple[int, str]] = []

    def revoke_user_sessions(self, user_id: int, reason: str) -> int:
        self.calls.append((user_id, reason))
        return self.counts.get(user_id, 0)


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
    display_name: str | None = None,
    description: str = "",
    declared: bool = True,
    enabled: bool = True,
    version: int = 1,
) -> Permission:
    permission = Permission(
        name=name,
        display_name=display_name or name,
        description=description,
        is_declared=declared,
        is_enabled=enabled,
        version=version,
    )
    db.add(permission)
    db.flush()
    return permission


def add_user(db: DbSession, email: str) -> User:
    user = User(email=email, hashed_password="hashed-password")
    db.add(user)
    db.flush()
    return user


def add_role(db: DbSession, name: str, *, enabled: bool = True) -> Role:
    role = Role(name=name, is_enabled=enabled)
    db.add(role)
    db.flush()
    return role


def attach_user_role(db: DbSession, user: User, role: Role) -> None:
    db.execute(user_roles.insert().values(user_id=user.id, role_id=role.id))


def add_endpoint(db: DbSession, permission: Permission, route_name: str) -> None:
    db.add(
        PermissionEndpoint(
            permission_id=permission.id,
            http_method="GET",
            path="/admin/apps",
            route_name=route_name,
        )
    )


def permission_audits(db: DbSession) -> list[AuditEvent]:
    return list(
        db.scalars(
            select(AuditEvent)
            .where(AuditEvent.target_type == "permission")
            .order_by(AuditEvent.id)
        )
    )


def test_list_and_detail_return_counts_filters_and_sorted_endpoints(db_session: DbSession) -> None:
    first = add_permission(db_session, "app:read", display_name="View apps", description="Applications")
    second = add_permission(db_session, "role:read", display_name="View roles")
    third = add_permission(db_session, "legacy:read", declared=False)
    underscored = add_permission(db_session, "user_admin:read")
    wildcard_neighbor = add_permission(db_session, "userxadmin:read")
    role = add_role(db_session, "reader")
    db_session.execute(role_permissions.insert().values(role_id=role.id, permission_id=first.id))
    add_endpoint(db_session, first, "z_route")
    db_session.add(
        PermissionEndpoint(
            permission_id=first.id,
            http_method="POST",
            path="/admin/apps",
            route_name="a_route",
        )
    )
    db_session.commit()

    service = AdminPermissionService(db_session)
    records, total = service.list_permissions(page=1, page_size=1, keyword="APPLICATIONS")

    assert total == 1
    assert records[0].permission.id == first.id
    assert records[0].endpoint_count == 2
    assert records[0].role_count == 1
    assert service.list_permissions(resource="role")[0][0].permission.id == second.id
    assert service.list_permissions(is_declared=False)[0][0].permission.id == third.id
    resource_records, resource_total = service.list_permissions(resource="user_admin")
    assert resource_total == 1
    assert resource_records[0].permission.id == underscored.id
    assert wildcard_neighbor.id != underscored.id

    detail = service.get_permission(first.id)
    assert [endpoint.route_name for endpoint in detail.endpoints] == ["z_route", "a_route"]
    assert detail.endpoint_count == 2
    assert detail.role_count == 1

    with pytest.raises(PermissionNotFoundError, match="PERMISSION_NOT_FOUND"):
        service.get_permission(999)


def test_update_uses_version_is_idempotent_and_audits_only_real_changes(db_session: DbSession) -> None:
    actor = add_user(db_session, "actor@example.com")
    permission = add_permission(db_session, "app:read", description="Old")
    db_session.commit()
    service = AdminPermissionService(db_session)

    updated, changed, revoked = service.update_permission(
        permission.id,
        AdminPermissionUpdate(display_name="View apps", description="New", version=1),
        actor_user_id=actor.id,
        request_id="req-permission-update",
    )

    assert changed is True
    assert revoked == 0
    assert updated.permission.display_name == "View apps"
    assert updated.permission.version == 2
    assert permission_audits(db_session)[0].action == "permission.updated"

    unchanged, changed, revoked = service.update_permission(
        permission.id,
        AdminPermissionUpdate(display_name="View apps", description="New", version=2),
        actor_user_id=actor.id,
    )
    assert changed is False
    assert revoked == 0
    assert unchanged.permission.version == 2
    assert len(permission_audits(db_session)) == 1

    with pytest.raises(PermissionVersionConflictError, match="PERMISSION_VERSION_CONFLICT"):
        service.update_permission(
            permission.id,
            AdminPermissionUpdate(description="stale", version=1),
            actor_user_id=actor.id,
        )
    assert len(permission_audits(db_session)) == 1


def test_update_maps_deleted_or_concurrent_row_conflict(monkeypatch: pytest.MonkeyPatch, db_session: DbSession) -> None:
    actor = add_user(db_session, "actor@example.com")
    permission = add_permission(db_session, "app:read")
    db_session.commit()
    service = AdminPermissionService(db_session)
    original_execute = db_session.execute

    class EmptyResult:
        rowcount = 0

    def execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
        if statement.is_update:
            return EmptyResult()
        return original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "execute", execute)
    with pytest.raises(PermissionVersionConflictError):
        service.update_permission(
            permission.id,
            AdminPermissionUpdate(display_name="changed", version=1),
            actor_user_id=actor.id,
        )


def test_disable_revokes_distinct_users_only_through_enabled_roles_and_preserves_data(
    db_session: DbSession,
    fake_redis: FakeRedis,
) -> None:
    actor = add_user(db_session, "actor@example.com")
    first_user = add_user(db_session, "first@example.com")
    second_user = add_user(db_session, "second@example.com")
    disabled_role_user = add_user(db_session, "disabled-role@example.com")
    permission = add_permission(db_session, "app:read")
    first_role = add_role(db_session, "reader")
    second_role = add_role(db_session, "auditor")
    disabled_role = add_role(db_session, "archived", enabled=False)
    for user, role in (
        (first_user, first_role),
        (first_user, second_role),
        (second_user, first_role),
        (disabled_role_user, disabled_role),
    ):
        attach_user_role(db_session, user, role)
    for role in (first_role, second_role, disabled_role):
        db_session.execute(
            role_permissions.insert().values(role_id=role.id, permission_id=permission.id)
        )
    db_session.add_all(
        (
            AuthSession(sid="sid-first", user_id=first_user.id, status="active"),
            AuthSession(sid="sid-second", user_id=second_user.id, status="active"),
            AuthSession(sid="sid-disabled-role", user_id=disabled_role_user.id, status="active"),
        )
    )
    db_session.commit()

    service = AdminPermissionService(db_session)
    result, changed, revoked = service.disable_permission(
        permission.id,
        actor_user_id=actor.id,
        reason="  maintenance  ",
        request_id="req-permission-disable",
    )
    db_session.commit()

    assert changed is True
    assert revoked == 2
    assert result.permission.is_enabled is False
    assert result.permission.disabled_reason == "maintenance"
    assert result.permission.version == 2
    assert db_session.scalar(select(func.count()).select_from(PermissionEndpoint)) == 0
    sessions = db_session.scalars(select(AuthSession).order_by(AuthSession.id)).all()
    assert [session.status for session in sessions] == ["revoked", "revoked", "active"]
    assert set(fake_redis.values.values()) == {"revoked"}
    assert {audit.action for audit in permission_audits(db_session)} == {"permission.disabled"}
    assert "sid-first" not in str(permission_audits(db_session)[0].changes_json)

    repeated, changed, revoked = service.disable_permission(
        permission.id,
        actor_user_id=actor.id,
        reason="replace me",
    )
    assert changed is False
    assert revoked == 0
    assert repeated.permission.disabled_reason == "maintenance"
    assert len(permission_audits(db_session)) == 1


def test_enable_clears_state_but_is_idempotent_and_does_not_revoke(db_session: DbSession) -> None:
    actor = add_user(db_session, "actor@example.com")
    permission = add_permission(db_session, "app:read", enabled=False)
    permission.disabled_at = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
    permission.disabled_reason = "maintenance"
    db_session.commit()
    service = AdminPermissionService(db_session)

    enabled, changed, revoked = service.enable_permission(
        permission.id,
        actor_user_id=actor.id,
        request_id="req-permission-enable",
    )
    db_session.commit()

    assert changed is True
    assert revoked == 0
    assert enabled.permission.is_enabled is True
    assert enabled.permission.disabled_at is None
    assert enabled.permission.disabled_reason is None
    assert enabled.permission.version == 2
    assert permission_audits(db_session)[0].action == "permission.enabled"

    repeated, changed, revoked = service.enable_permission(permission.id, actor_user_id=actor.id)
    assert changed is False
    assert revoked == 0
    assert repeated.permission.version == 2
    assert len(permission_audits(db_session)) == 1


def test_core_enable_permission_and_missing_permission_are_protected(db_session: DbSession) -> None:
    actor = add_user(db_session, "actor@example.com")
    core = add_permission(db_session, "permission:enable")
    missing = add_permission(db_session, "legacy:read", declared=False, enabled=False)
    db_session.commit()
    service = AdminPermissionService(db_session)

    with pytest.raises(ProtectedPermissionOperationError, match="PROTECTED_PERMISSION_OPERATION"):
        service.disable_permission(core.id, actor_user_id=actor.id)
    with pytest.raises(PermissionNotDeclaredError, match="PERMISSION_NOT_DECLARED"):
        service.enable_permission(missing.id, actor_user_id=actor.id)
    assert permission_audits(db_session) == []


def test_disable_change_and_audit_share_transaction_and_rollback(db_session: DbSession) -> None:
    actor = add_user(db_session, "actor@example.com")
    permission = add_permission(db_session, "app:read")
    db_session.commit()
    service = AdminPermissionService(db_session)

    service.disable_permission(permission.id, actor_user_id=actor.id, reason="rollback")
    assert permission_audits(db_session)
    db_session.rollback()

    restored = db_session.get(Permission, permission.id)
    assert restored is not None
    assert restored.is_enabled is True
    assert restored.version == 1
    assert permission_audits(db_session) == []


def test_status_operations_issue_for_update(db_session: DbSession) -> None:
    actor = add_user(db_session, "actor@example.com")
    permission = add_permission(db_session, "app:read")
    db_session.commit()
    statements: list[Any] = []
    original_scalar = db_session.scalar

    def scalar_spy(statement: Any, *args: Any, **kwargs: Any) -> Any:
        statements.append(statement)
        return original_scalar(statement, *args, **kwargs)

    db_session.scalar = scalar_spy  # type: ignore[method-assign]
    service = AdminPermissionService(db_session)
    service.disable_permission(permission.id, actor_user_id=actor.id)
    service.enable_permission(permission.id, actor_user_id=actor.id)

    lock_statements = [
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in statements
        if getattr(statement, "_for_update_arg", None) is not None
    ]
    assert len(lock_statements) == 2
    assert all("FOR UPDATE" in statement for statement in lock_statements)
