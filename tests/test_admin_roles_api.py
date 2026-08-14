from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session as DbSession, sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.admin_roles as api_module
import app.api.dependencies as dependencies
import app.services.session_service as session_module
from app.api.admin_roles import router as admin_roles_router
from app.core.database import Base, get_db
from app.core.logging import RequestIdMiddleware
from app.main import app as main_app
from app.models.audit_event import AuditEvent
from app.models.permission import Permission
from app.models.role import Role, role_permissions, user_roles
from app.models.session import Session as AuthSession
from app.models.user import User
from app.services.admin_role_service import RoleNameAlreadyExistsError
from app.services.authorization_service import AuthenticationError, PermissionDeniedError

AUTH_HEADERS = {"Authorization": "Bearer access"}


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value

    def get(self, key: str) -> str | None:
        return self.values.get(key)


@pytest.fixture
def api_context(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, DbSession, list[tuple[str, ...]], dict[str, bool], FakeRedis]]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = TestingSessionLocal()
    actor = User(email="admin@example.com", hashed_password="hashed-password", is_active=True)
    target = User(
        email="target@example.com",
        display_name="Target Reviewer",
        hashed_password="target-hashed-password",
    )
    db.add_all([actor, target])
    db.commit()
    db.refresh(actor)
    actor_id = actor.id
    permission_calls: list[tuple[str, ...]] = []
    auth_state = {"allowed": True}
    redis = FakeRedis()

    class AuthorizationStub:
        def __init__(self, session: DbSession) -> None:
            self.db = session

        def require_permissions(self, access_token: str, required_permissions: tuple[str, ...]) -> User:
            if access_token != "access":
                raise AuthenticationError("invalid access token")
            permission_calls.append(required_permissions)
            if not auth_state["allowed"]:
                raise PermissionDeniedError("insufficient permissions")
            authenticated_actor = self.db.get(User, actor_id)
            assert authenticated_actor is not None
            return authenticated_actor

    monkeypatch.setattr(dependencies, "AuthorizationService", AuthorizationStub)
    monkeypatch.setattr(session_module, "get_redis", lambda: redis)

    def override_db() -> Iterator[DbSession]:
        yield db

    test_app = FastAPI()
    test_app.add_middleware(RequestIdMiddleware)
    test_app.include_router(admin_roles_router)
    test_app.dependency_overrides[get_db] = override_db

    try:
        with TestClient(test_app) as client:
            yield client, db, permission_calls, auth_state, redis
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def role_by_name(db: DbSession, name: str) -> Role:
    role = db.scalar(select(Role).where(Role.name == name))
    assert role is not None
    return role


def user_by_email(db: DbSession, email: str) -> User:
    user = db.scalar(select(User).where(User.email == email))
    assert user is not None
    return user


def assert_permission(permission_calls: list[tuple[str, ...]], expected: str) -> None:
    assert permission_calls[-1] == (expected,)


def test_admin_role_routes_are_registered_with_security_and_expected_methods() -> None:
    paths = main_app.openapi()["paths"]

    assert set(paths["/admin/roles"]) >= {"get", "post"}
    assert set(paths["/admin/roles/{role_id}"]) >= {"get", "patch"}
    assert "post" in paths["/admin/roles/{role_id}/disable"]
    assert "post" in paths["/admin/roles/{role_id}/enable"]
    assert "get" in paths["/admin/roles/{role_id}/users"]
    assert set(paths["/admin/roles/{role_id}/permissions"]) >= {"get", "put"}
    for path, method in (
        ("/admin/roles", "get"),
        ("/admin/roles", "post"),
        ("/admin/roles/{role_id}", "patch"),
        ("/admin/roles/{role_id}/users", "get"),
        ("/admin/roles/{role_id}/permissions", "get"),
        ("/admin/roles/{role_id}/permissions", "put"),
    ):
        assert paths[path][method]["security"]


def test_admin_role_routes_enforce_authentication_and_authorization(api_context) -> None:
    client, _db, _permission_calls, auth_state, _redis = api_context

    unauthenticated = client.get("/admin/roles")
    auth_state["allowed"] = False
    forbidden = client.get("/admin/roles", headers=AUTH_HEADERS)

    assert unauthenticated.status_code == 401
    assert unauthenticated.json() == {"detail": "invalid access token"}
    assert forbidden.status_code == 403
    assert forbidden.json() == {"detail": "insufficient permissions"}


def test_role_lifecycle_uses_independent_permissions_and_safe_responses(api_context) -> None:
    client, db, permission_calls, _auth_state, redis = api_context

    created = client.post(
        "/admin/roles",
        headers={**AUTH_HEADERS, "X-Request-ID": "req-role-create-api"},
        json={"name": "  auditor  ", "description": "  Audit access  "},
    )

    assert created.status_code == 201
    created_body = created.json()
    role_id = created_body["id"]
    assert created_body["name"] == "auditor"
    assert created_body["description"] == "Audit access"
    assert created_body["is_enabled"] is True
    assert created_body["version"] == 1
    assert_permission(permission_calls, "role:create")

    listed = client.get(
        "/admin/roles",
        headers=AUTH_HEADERS,
        params={"keyword": "AUDIT", "is_enabled": True, "page_size": 1},
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["id"] == role_id
    assert_permission(permission_calls, "role:read")

    detail = client.get(f"/admin/roles/{role_id}", headers=AUTH_HEADERS)
    assert detail.status_code == 200
    assert detail.json()["name"] == "auditor"
    assert "permissions" not in detail.json()
    assert_permission(permission_calls, "role:read")

    updated = client.patch(
        f"/admin/roles/{role_id}",
        headers={**AUTH_HEADERS, "X-Request-ID": "req-role-update-api"},
        json={"description": "Review access", "version": created_body["version"]},
    )
    assert updated.status_code == 200
    assert updated.json()["changed"] is True
    assert updated.json()["revoked_sessions"] == 0
    assert updated.json()["description"] == "Review access"
    assert updated.json()["version"] == 2
    assert_permission(permission_calls, "role:update")

    target = user_by_email(db, "target@example.com")
    db.execute(user_roles.insert().values(user_id=target.id, role_id=role_id))
    db.add(AuthSession(sid="sid-role-api", user_id=target.id, status="active"))
    db.commit()

    associated = client.get(
        f"/admin/roles/{role_id}/users",
        headers=AUTH_HEADERS,
        params={"keyword": "REVIEWER", "is_active": True, "is_blacklisted": False},
    )
    assert associated.status_code == 200
    assert associated.json()["total"] == 1
    assert associated.json()["items"][0]["email"] == "target@example.com"
    assert "hashed_password" not in str(associated.json())
    assert_permission(permission_calls, "role:read")

    disabled = client.post(
        f"/admin/roles/{role_id}/disable",
        headers={**AUTH_HEADERS, "X-Request-ID": "req-role-disable-api"},
        json={"reason": "maintenance"},
    )
    assert disabled.status_code == 200
    assert disabled.json()["changed"] is True
    assert disabled.json()["is_enabled"] is False
    assert disabled.json()["disabled_reason"] == "maintenance"
    assert disabled.json()["revoked_sessions"] == 1
    assert_permission(permission_calls, "role:disable")
    assert "revoked" in redis.values.values()

    repeated_disable = client.post(
        f"/admin/roles/{role_id}/disable",
        headers=AUTH_HEADERS,
        json={"reason": "must not replace"},
    )
    assert repeated_disable.status_code == 200
    assert repeated_disable.json()["changed"] is False
    assert repeated_disable.json()["disabled_reason"] == "maintenance"
    assert repeated_disable.json()["version"] == 3

    enabled = client.post(f"/admin/roles/{role_id}/enable", headers=AUTH_HEADERS)
    assert enabled.status_code == 200
    assert enabled.json()["changed"] is True
    assert enabled.json()["is_enabled"] is True
    assert enabled.json()["disabled_at"] is None
    assert enabled.json()["disabled_reason"] is None
    assert enabled.json()["version"] == 4
    assert_permission(permission_calls, "role:enable")

    audits = db.scalars(
        select(AuditEvent).where(AuditEvent.target_type == "role").order_by(AuditEvent.id)
    ).all()
    assert [audit.action for audit in audits] == [
        "role.created",
        "role.updated",
        "role.disabled",
        "role.enabled",
    ]
    assert audits[0].request_id == "req-role-create-api"
    assert audits[1].request_id == "req-role-update-api"
    assert audits[2].request_id == "req-role-disable-api"
    audit_text = str([audit.changes_json for audit in audits]).lower()
    assert "password" not in audit_text
    assert "token" not in audit_text
    assert "sid-role-api" not in audit_text


def test_role_permissions_query_and_full_replacement_are_safe_and_audited(
    api_context,
) -> None:
    client, db, permission_calls, _auth_state, redis = api_context
    role = Role(name="permission-role", description="Managed permissions")
    app_read = Permission(
        name="app:read",
        display_name="View apps",
        description="Applications",
    )
    missing = Permission(
        name="legacy:read",
        display_name="Legacy read",
        description="Historical",
        is_declared=False,
        is_enabled=False,
    )
    db.add_all((role, app_read, missing))
    db.flush()
    db.execute(
        role_permissions.insert().values(
            role_id=role.id,
            permission_id=missing.id,
        )
    )
    target = user_by_email(db, "target@example.com")
    db.execute(user_roles.insert().values(user_id=target.id, role_id=role.id))
    db.add(AuthSession(sid="sid-role-permissions-api", user_id=target.id, status="active"))
    db.commit()

    queried = client.get(
        f"/admin/roles/{role.id}/permissions",
        headers=AUTH_HEADERS,
    )
    assert queried.status_code == 200
    assert queried.json() == {
        "role_id": role.id,
        "permissions": [
            {
                "id": missing.id,
                "name": "legacy:read",
                "display_name": "Legacy read",
                "description": "Historical",
                "is_declared": False,
                "is_enabled": False,
            }
        ],
        "version": 1,
        "changed": False,
        "revoked_sessions": 0,
    }
    assert_permission(permission_calls, "role:read")

    assigned = client.put(
        f"/admin/roles/{role.id}/permissions",
        headers={**AUTH_HEADERS, "X-Request-ID": "req-role-permissions-api"},
        json={
            "permission_ids": [missing.id, app_read.id],
            "version": role.version,
        },
    )
    assert assigned.status_code == 200
    assigned_body = assigned.json()
    assert assigned_body["changed"] is True
    assert assigned_body["revoked_sessions"] == 1
    assert assigned_body["version"] == 2
    assert [permission["name"] for permission in assigned_body["permissions"]] == [
        "app:read",
        "legacy:read",
    ]
    assert_permission(permission_calls, "role:assign_permissions")
    assert "revoked" in redis.values.values()
    assert all(
        field not in str(assigned_body).lower()
        for field in ("endpoint", "password", "token", "sid")
    )

    repeated = client.put(
        f"/admin/roles/{role.id}/permissions",
        headers=AUTH_HEADERS,
        json={
            "permission_ids": [app_read.id, missing.id],
            "version": assigned_body["version"],
        },
    )
    assert repeated.status_code == 200
    assert repeated.json()["changed"] is False
    assert repeated.json()["version"] == 2
    assert repeated.json()["revoked_sessions"] == 0

    audit = db.scalar(
        select(AuditEvent).where(AuditEvent.action == "role.permissions_assigned")
    )
    assert audit is not None
    assert audit.request_id == "req-role-permissions-api"
    assert audit.changes_json["permissions"]["from"] == [
        {"id": missing.id, "name": "legacy:read"}
    ]
    assert audit.changes_json["permissions"]["to"] == [
        {"id": app_read.id, "name": "app:read"},
        {"id": missing.id, "name": "legacy:read"},
    ]
    assert "sid-role-permissions-api" not in str(audit.changes_json)


def test_role_permission_api_maps_domain_and_validation_errors(api_context) -> None:
    client, db, _permission_calls, _auth_state, _redis = api_context
    role = Role(name="permission-errors")
    disabled = Permission(
        name="app:update",
        display_name="Update apps",
        is_enabled=False,
    )
    missing = Permission(
        name="legacy:read",
        display_name="Legacy read",
        is_declared=False,
    )
    db.add_all((role, disabled, missing))
    db.commit()

    missing_role = client.get("/admin/roles/999/permissions", headers=AUTH_HEADERS)
    missing_permission = client.put(
        f"/admin/roles/{role.id}/permissions",
        headers=AUTH_HEADERS,
        json={"permission_ids": [999], "version": role.version},
    )
    disabled_permission = client.put(
        f"/admin/roles/{role.id}/permissions",
        headers=AUTH_HEADERS,
        json={"permission_ids": [disabled.id], "version": role.version},
    )
    undeclared_permission = client.put(
        f"/admin/roles/{role.id}/permissions",
        headers=AUTH_HEADERS,
        json={"permission_ids": [missing.id], "version": role.version},
    )
    stale = client.put(
        f"/admin/roles/{role.id}/permissions",
        headers=AUTH_HEADERS,
        json={"permission_ids": [], "version": 999},
    )
    duplicate_ids = client.put(
        f"/admin/roles/{role.id}/permissions",
        headers=AUTH_HEADERS,
        json={"permission_ids": [disabled.id, disabled.id], "version": role.version},
    )

    assert missing_role.status_code == 404
    assert missing_role.json() == {"detail": "ROLE_NOT_FOUND"}
    assert missing_permission.status_code == 404
    assert missing_permission.json() == {"detail": "PERMISSION_NOT_FOUND"}
    assert disabled_permission.status_code == 409
    assert disabled_permission.json() == {"detail": "PERMISSION_DISABLED"}
    assert undeclared_permission.status_code == 409
    assert undeclared_permission.json() == {"detail": "PERMISSION_NOT_DECLARED"}
    assert stale.status_code == 409
    assert stale.json() == {"detail": "ROLE_VERSION_CONFLICT"}
    assert duplicate_ids.status_code == 422
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(role_permissions)) == 0
    assert db.get(Role, role.id).version == 1
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_role_api_maps_domain_and_validation_errors(api_context) -> None:
    client, _db, _permission_calls, _auth_state, _redis = api_context
    created = client.post("/admin/roles", headers=AUTH_HEADERS, json={"name": "auditor"})
    role_id = created.json()["id"]
    client.post("/admin/roles", headers=AUTH_HEADERS, json={"name": "admin"})

    missing = client.get("/admin/roles/999", headers=AUTH_HEADERS)
    missing_users = client.get("/admin/roles/999/users", headers=AUTH_HEADERS)
    duplicate = client.post("/admin/roles", headers=AUTH_HEADERS, json={"name": "auditor"})
    stale = client.patch(
        f"/admin/roles/{role_id}",
        headers=AUTH_HEADERS,
        json={"description": "stale", "version": 999},
    )
    protected = client.post(
        f"/admin/roles/{role_by_name(_db, 'admin').id}/disable",
        headers=AUTH_HEADERS,
        json={},
    )
    invalid = client.post(
        "/admin/roles",
        headers=AUTH_HEADERS,
        json={"name": "   ", "is_enabled": False},
    )

    assert missing.status_code == 404
    assert missing.json() == {"detail": "ROLE_NOT_FOUND"}
    assert missing_users.status_code == 404
    assert missing_users.json() == {"detail": "ROLE_NOT_FOUND"}
    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "ROLE_NAME_ALREADY_EXISTS"}
    assert stale.status_code == 409
    assert stale.json() == {"detail": "ROLE_VERSION_CONFLICT"}
    assert protected.status_code == 409
    assert protected.json() == {"detail": "PROTECTED_ROLE_OPERATION"}
    assert invalid.status_code == 422


def test_role_permission_write_failure_rolls_back_association_version_and_audit(
    api_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, db, _permission_calls, _auth_state, _redis = api_context
    role = Role(name="permission-rollback")
    permission = Permission(name="app:read", display_name="View apps")
    db.add_all((role, permission))
    db.commit()

    def fail_after_flush(
        service: api_module.AdminRoleService,
        role_id: int,
        permission_ids: list[int],
        version: int,
        *,
        actor_user_id: int,
        request_id: str | None = None,
    ) -> tuple[Role, list[Permission], bool, int]:
        target = service.db.get(Role, role_id)
        assigned = service.db.get(Permission, permission_ids[0])
        assert target is not None
        assert assigned is not None
        service.db.execute(
            role_permissions.insert().values(
                role_id=target.id,
                permission_id=assigned.id,
            )
        )
        target.version = version + 1
        service.db.add(
            AuditEvent(
                actor_user_id=actor_user_id,
                action="role.permissions_assigned",
                target_type="role",
                target_id=target.id,
                result="success",
                changes_json={},
                request_id=request_id or "unknown",
            )
        )
        service.db.flush()
        raise api_module.RoleVersionConflictError(
            api_module.RoleVersionConflictError.code
        )

    monkeypatch.setattr(
        api_module.AdminRoleService,
        "assign_permissions",
        fail_after_flush,
    )

    response = client.put(
        f"/admin/roles/{role.id}/permissions",
        headers={**AUTH_HEADERS, "X-Request-ID": "req-role-permission-rollback"},
        json={"permission_ids": [permission.id], "version": role.version},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "ROLE_VERSION_CONFLICT"}
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(role_permissions)) == 0
    assert db.get(Role, role.id).version == 1
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_role_write_domain_failure_rolls_back_business_data_and_audit(
    api_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, db, _permission_calls, _auth_state, _redis = api_context

    def fail_after_flush(
        service: api_module.AdminRoleService,
        payload: Any,
        *,
        actor_user_id: int,
        request_id: str | None = None,
    ) -> Role:
        role = Role(name=payload.name, description=payload.description)
        service.db.add(role)
        service.db.flush()
        service.db.add(
            AuditEvent(
                actor_user_id=actor_user_id,
                action="role.created",
                target_type="role",
                target_id=role.id,
                result="success",
                changes_json={},
                request_id=request_id or "unknown",
            )
        )
        service.db.flush()
        raise RoleNameAlreadyExistsError(RoleNameAlreadyExistsError.code)

    monkeypatch.setattr(api_module.AdminRoleService, "create_role", fail_after_flush)

    response = client.post(
        "/admin/roles",
        headers={**AUTH_HEADERS, "X-Request-ID": "req-role-rollback"},
        json={"name": "must-not-persist"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "ROLE_NAME_ALREADY_EXISTS"}
    assert db.scalar(select(func.count()).select_from(Role)) == 0
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0
