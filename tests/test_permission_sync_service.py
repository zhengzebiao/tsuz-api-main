from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.main import create_app
from app.models.permission import Permission
from app.models.permission_endpoint import PermissionEndpoint
from app.models.role import Role, role_permissions, user_roles
from app.models.user import User
from app.services.permission_scanner import (
    PermissionScanResult,
    ScannedPermissionBinding,
    scan_permission_routes,
)
from app.services.permission_sync_service import (
    AdminRoleRequiredError,
    PermissionSyncDialectError,
    PermissionSyncService,
)


class RecordingSessionService:
    def __init__(self, counts: dict[int, int] | None = None) -> None:
        self.counts = counts or {}
        self.calls: list[tuple[int, str]] = []

    def revoke_user_sessions(self, user_id: int, reason: str) -> int:
        self.calls.append((user_id, reason))
        return self.counts.get(user_id, 0)


class FailingSessionService:
    def revoke_user_sessions(self, user_id: int, reason: str) -> int:
        raise RuntimeError("redis unavailable")


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


def add_admin_role(db: DbSession, *, with_user: bool = False) -> tuple[Role, User | None]:
    role = Role(name="admin")
    db.add(role)
    db.flush()
    if not with_user:
        db.commit()
        return role, None

    user = User(email="admin@example.com", hashed_password="hashed-password")
    db.add(user)
    db.flush()
    db.execute(user_roles.insert().values(user_id=user.id, role_id=role.id))
    db.commit()
    return role, user


def scan_result(
    *bindings: tuple[str, str, str, str],
    permission_names: tuple[str, ...] | None = None,
) -> PermissionScanResult:
    scanned_bindings = tuple(
        sorted(
            ScannedPermissionBinding(
                permission_name=permission_name,
                http_method=http_method,
                path=path,
                route_name=route_name,
            )
            for permission_name, http_method, path, route_name in bindings
        )
    )
    names = permission_names or tuple(
        sorted({binding.permission_name for binding in scanned_bindings})
    )
    return PermissionScanResult(
        permission_names=names,
        bindings=scanned_bindings,
        routes=(),
    )


def make_service(
    db: DbSession,
    sessions: RecordingSessionService | FailingSessionService | None = None,
    *,
    now: datetime | None = None,
) -> PermissionSyncService:
    service = PermissionSyncService(
        db,
        session_service=sessions,
        now=(lambda: now) if now is not None else None,
    )
    service._acquire_advisory_lock = lambda: None  # type: ignore[method-assign]
    return service


def test_first_sync_creates_real_catalog_bindings_and_admin_grants_idempotently(
    db_session: DbSession,
) -> None:
    _admin_role, admin_user = add_admin_role(db_session, with_user=True)
    assert admin_user is not None
    sessions = RecordingSessionService({admin_user.id: 2})
    service = make_service(db_session, sessions)
    catalog = scan_permission_routes(create_app())

    plan = service.build_plan(catalog)

    assert len(plan.created) == 26
    assert len(plan.endpoint_bindings_added) == 33
    assert len(plan.admin_grants_added) == 26
    assert plan.session_user_ids == (admin_user.id,)
    assert db_session.scalar(select(func.count()).select_from(Permission)) == 0

    summary = service.apply_plan(plan)
    db_session.commit()

    assert summary.to_dict() == {
        "created": 26,
        "restored": 0,
        "marked_missing": 0,
        "endpoint_bindings_added": 33,
        "endpoint_bindings_removed": 0,
        "admin_grants_added": 26,
        "sessions_revoked": 2,
        "unchanged": 0,
    }
    assert db_session.scalar(select(func.count()).select_from(Permission)) == 26
    assert db_session.scalar(select(func.count()).select_from(PermissionEndpoint)) == 33
    assert db_session.scalar(select(func.count()).select_from(role_permissions)) == 26
    permissions = db_session.scalars(select(Permission).order_by(Permission.name)).all()
    assert all(permission.display_name == permission.name for permission in permissions)
    assert all(permission.description == "" for permission in permissions)
    assert all(permission.is_declared and permission.is_enabled for permission in permissions)
    assert all(permission.version == 1 for permission in permissions)

    versions = {permission.id: permission.version for permission in permissions}
    second_plan = service.build_plan(catalog)
    second_summary = service.apply_plan(second_plan)
    db_session.commit()

    assert second_plan.has_changes is False
    assert second_summary.to_dict() == {
        "created": 0,
        "restored": 0,
        "marked_missing": 0,
        "endpoint_bindings_added": 0,
        "endpoint_bindings_removed": 0,
        "admin_grants_added": 0,
        "sessions_revoked": 0,
        "unchanged": 26,
    }
    assert {permission.id: permission.version for permission in permissions} == versions
    assert sessions.calls == [(admin_user.id, "permission_sync")]


def test_sync_preserves_admin_managed_permission_fields(db_session: DbSession) -> None:
    admin_role, _ = add_admin_role(db_session)
    disabled_at = datetime(2026, 8, 13, 9, 30, tzinfo=UTC).replace(tzinfo=None)
    permission = Permission(
        name="app:read",
        display_name="应用查询",
        description="管理员说明",
        is_declared=True,
        is_enabled=False,
        disabled_at=disabled_at,
        disabled_reason="maintenance",
        version=7,
    )
    db_session.add(permission)
    db_session.flush()
    db_session.execute(
        role_permissions.insert().values(
            role_id=admin_role.id,
            permission_id=permission.id,
        )
    )
    db_session.commit()
    service = make_service(db_session)

    plan = service.build_plan(
        scan_result(("app:read", "GET", "/admin/apps", "list_apps"))
    )
    summary = service.apply_plan(plan)
    db_session.commit()

    db_session.refresh(permission)
    assert summary.endpoint_bindings_added == 1
    assert permission.display_name == "应用查询"
    assert permission.description == "管理员说明"
    assert permission.is_enabled is False
    assert permission.disabled_at == disabled_at
    assert permission.disabled_reason == "maintenance"
    assert permission.version == 8


def test_missing_and_restore_preserve_id_roles_and_first_missing_time(
    db_session: DbSession,
) -> None:
    add_admin_role(db_session)
    role = Role(name="reader")
    user = User(email="reader@example.com", hashed_password="hashed-password")
    permission = Permission(name="legacy:read", display_name="Legacy Read")
    db_session.add_all((role, user, permission))
    db_session.flush()
    permission_id = permission.id
    db_session.execute(
        role_permissions.insert().values(
            role_id=role.id,
            permission_id=permission.id,
        )
    )
    db_session.execute(user_roles.insert().values(user_id=user.id, role_id=role.id))
    db_session.add(
        PermissionEndpoint(
            permission_id=permission.id,
            http_method="GET",
            path="/admin/legacy",
            route_name="legacy",
        )
    )
    db_session.commit()

    missing_at = datetime(2026, 8, 13, 10, 0, tzinfo=UTC).replace(tzinfo=None)
    sessions = RecordingSessionService({user.id: 1})
    service = make_service(db_session, sessions, now=missing_at)
    missing_plan = service.build_plan(scan_result(permission_names=()))
    missing_summary = service.apply_plan(missing_plan)
    db_session.commit()

    db_session.refresh(permission)
    assert missing_plan.marked_missing == ("legacy:read",)
    assert missing_summary.endpoint_bindings_removed == 1
    assert missing_summary.sessions_revoked == 1
    assert permission.id == permission_id
    assert permission.is_declared is False
    assert permission.missing_at == missing_at
    assert db_session.scalar(select(func.count()).select_from(role_permissions)) == 1
    assert db_session.scalar(select(func.count()).select_from(PermissionEndpoint)) == 0

    first_missing_version = permission.version
    later = datetime(2026, 8, 13, 12, 0, tzinfo=UTC).replace(tzinfo=None)
    repeated_service = make_service(db_session, sessions, now=later)
    repeated_plan = repeated_service.build_plan(scan_result(permission_names=()))
    repeated_service.apply_plan(repeated_plan)
    db_session.commit()
    db_session.refresh(permission)
    assert repeated_plan.has_changes is False
    assert permission.missing_at == missing_at
    assert permission.version == first_missing_version

    restore_catalog = scan_result(
        ("legacy:read", "GET", "/admin/legacy", "legacy")
    )
    restore_plan = repeated_service.build_plan(restore_catalog)
    restore_summary = repeated_service.apply_plan(restore_plan)
    db_session.commit()

    db_session.refresh(permission)
    assert restore_plan.restored == ("legacy:read",)
    assert restore_summary.sessions_revoked == 1
    assert permission.id == permission_id
    assert permission.is_declared is True
    assert permission.missing_at is None
    assert db_session.scalar(select(func.count()).select_from(role_permissions)) == 2
    assert db_session.scalar(select(func.count()).select_from(PermissionEndpoint)) == 1


def test_restore_does_not_enable_an_admin_disabled_permission(db_session: DbSession) -> None:
    add_admin_role(db_session)
    permission = Permission(
        name="legacy:read",
        display_name="Legacy Read",
        is_declared=False,
        is_enabled=False,
        disabled_reason="operator disabled",
        missing_at=datetime(2026, 8, 12, 8, 0, tzinfo=UTC).replace(tzinfo=None),
    )
    db_session.add(permission)
    db_session.commit()
    sessions = RecordingSessionService()
    service = make_service(db_session, sessions)

    plan = service.build_plan(
        scan_result(("legacy:read", "GET", "/admin/legacy", "legacy"))
    )
    service.apply_plan(plan)
    db_session.commit()

    db_session.refresh(permission)
    assert permission.is_declared is True
    assert permission.is_enabled is False
    assert permission.disabled_reason == "operator disabled"
    assert sessions.calls == []


def test_endpoint_route_name_change_updates_once_without_session_revocation(
    db_session: DbSession,
) -> None:
    admin_role, _ = add_admin_role(db_session)
    permission = Permission(name="app:read", display_name="app:read", version=3)
    db_session.add(permission)
    db_session.flush()
    db_session.execute(
        role_permissions.insert().values(
            role_id=admin_role.id,
            permission_id=permission.id,
        )
    )
    db_session.add(
        PermissionEndpoint(
            permission_id=permission.id,
            http_method="GET",
            path="/admin/apps",
            route_name="old_name",
        )
    )
    db_session.commit()
    sessions = RecordingSessionService()
    service = make_service(db_session, sessions)

    plan = service.build_plan(
        scan_result(("app:read", "GET", "/admin/apps", "new_name"))
    )
    summary = service.apply_plan(plan)
    db_session.commit()

    db_session.refresh(permission)
    endpoint = db_session.scalar(select(PermissionEndpoint))
    assert endpoint is not None
    assert endpoint.route_name == "new_name"
    assert summary.endpoint_bindings_added == 1
    assert summary.endpoint_bindings_removed == 1
    assert permission.version == 4
    assert sessions.calls == []


def test_session_users_are_deduplicated_across_roles_and_admin_grants(
    db_session: DbSession,
) -> None:
    admin_role, admin_user = add_admin_role(db_session, with_user=True)
    assert admin_user is not None
    other_role = Role(name="other")
    permission = Permission(name="legacy:read", display_name="legacy:read")
    db_session.add_all((other_role, permission))
    db_session.flush()
    db_session.execute(
        user_roles.insert().values(user_id=admin_user.id, role_id=other_role.id)
    )
    db_session.execute(
        role_permissions.insert().values(
            role_id=other_role.id,
            permission_id=permission.id,
        )
    )
    db_session.commit()
    sessions = RecordingSessionService({admin_user.id: 3})
    service = make_service(db_session, sessions)

    plan = service.build_plan(
        scan_result(("new:read", "GET", "/admin/new", "new"))
    )
    summary = service.apply_plan(plan)
    db_session.commit()

    assert plan.marked_missing == ("legacy:read",)
    assert plan.admin_grants_added == ("new:read",)
    assert plan.session_user_ids == (admin_user.id,)
    assert summary.sessions_revoked == 3
    assert sessions.calls == [(admin_user.id, "permission_sync")]
    assert admin_role.id is not None


def test_redis_failure_can_be_rolled_back_and_retried(db_session: DbSession) -> None:
    add_admin_role(db_session, with_user=True)
    catalog = scan_result(("app:read", "GET", "/admin/apps", "list_apps"))
    failing_service = make_service(db_session, FailingSessionService())
    plan = failing_service.build_plan(catalog)

    with pytest.raises(RuntimeError, match="redis unavailable"):
        failing_service.apply_plan(plan)
    db_session.rollback()

    assert db_session.scalar(select(func.count()).select_from(Permission)) == 0
    assert db_session.scalar(select(func.count()).select_from(PermissionEndpoint)) == 0
    assert db_session.scalar(select(func.count()).select_from(role_permissions)) == 0

    retry_service = make_service(db_session, RecordingSessionService())
    retry_summary = retry_service.apply_plan(retry_service.build_plan(catalog))
    db_session.commit()
    assert retry_summary.created == 1
    assert db_session.scalar(select(func.count()).select_from(Permission)) == 1


def test_build_plan_requires_seeded_admin_role_without_writing(db_session: DbSession) -> None:
    service = make_service(db_session)

    with pytest.raises(AdminRoleRequiredError, match="admin role"):
        service.build_plan(
            scan_result(("app:read", "GET", "/admin/apps", "list_apps"))
        )

    assert not db_session.new
    assert not db_session.dirty
    assert db_session.scalar(select(func.count()).select_from(Permission)) == 0


def test_apply_plan_requires_postgresql_when_lock_is_not_replaced(
    db_session: DbSession,
) -> None:
    add_admin_role(db_session)
    service = PermissionSyncService(db_session, session_service=RecordingSessionService())
    plan = service.build_plan(
        scan_result(("app:read", "GET", "/admin/apps", "list_apps"))
    )

    with pytest.raises(PermissionSyncDialectError, match="PostgreSQL"):
        service.apply_plan(plan)

    assert db_session.scalar(select(func.count()).select_from(Permission)) == 0
