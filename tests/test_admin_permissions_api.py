from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.session_service as session_module
from app.api import dependencies
from app.api.admin_permissions import router as admin_permissions_router
from app.core.database import Base, get_db
from app.core.logging import RequestIdMiddleware
from app.models.audit_event import AuditEvent
from app.models.permission import Permission
from app.models.permission_endpoint import PermissionEndpoint
from app.models.role import Role, role_permissions, user_roles
from app.models.session import Session as AuthSession
from app.models.user import User
from app.services.authorization_service import AuthenticationError, PermissionDeniedError

AUTH_HEADERS = {"Authorization": "Bearer access"}


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
    target = User(email="target@example.com", hashed_password="target-password")
    db.add_all([actor, target])
    db.flush()
    app_read = Permission(
        name="app:read",
        display_name="View apps",
        description="Applications",
    )
    permission_enable = Permission(name="permission:enable", display_name="Enable permissions")
    db.add_all([app_read, permission_enable])
    db.flush()
    role = Role(name="reader")
    db.add(role)
    db.flush()
    db.execute(user_roles.insert().values(user_id=target.id, role_id=role.id))
    db.execute(role_permissions.insert().values(role_id=role.id, permission_id=app_read.id))
    db.add(
        PermissionEndpoint(
            permission_id=app_read.id,
            http_method="GET",
            path="/admin/apps",
            route_name="list_apps",
        )
    )
    db.add(AuthSession(sid="sid-target", user_id=target.id, status="active"))
    db.commit()
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
    test_app.include_router(admin_permissions_router)
    test_app.dependency_overrides[get_db] = override_db

    try:
        with TestClient(test_app, raise_server_exceptions=False) as client:
            yield client, db, permission_calls, auth_state, redis
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def permission_by_name(db: DbSession, name: str) -> Permission:
    permission = db.scalar(select(Permission).where(Permission.name == name))
    assert permission is not None
    return permission


def test_permission_routes_are_registered_with_security_and_no_create_delete() -> None:
    app = FastAPI()
    app.include_router(admin_permissions_router)
    paths = app.openapi()["paths"]

    assert set(paths["/admin/permissions"]) == {"get"}
    assert set(paths["/admin/permissions/{permission_id}"]) == {"get", "patch"}
    assert set(paths["/admin/permissions/{permission_id}/disable"]) == {"post"}
    assert set(paths["/admin/permissions/{permission_id}/enable"]) == {"post"}
    for path, method in (
        ("/admin/permissions", "get"),
        ("/admin/permissions/{permission_id}", "get"),
        ("/admin/permissions/{permission_id}", "patch"),
        ("/admin/permissions/{permission_id}/disable", "post"),
        ("/admin/permissions/{permission_id}/enable", "post"),
    ):
        assert paths[path][method]["security"]


def test_permission_routes_enforce_authentication_and_authorization(api_context) -> None:
    client, _db, _permission_calls, auth_state, _redis = api_context

    unauthenticated = client.get("/admin/permissions")
    auth_state["allowed"] = False
    forbidden = client.get("/admin/permissions", headers=AUTH_HEADERS)

    assert unauthenticated.status_code == 401
    assert unauthenticated.json() == {"detail": "invalid access token"}
    assert forbidden.status_code == 403
    assert forbidden.json() == {"detail": "insufficient permissions"}


def test_permission_lifecycle_uses_independent_permissions_and_safe_responses(api_context) -> None:
    client, db, permission_calls, _auth_state, redis = api_context
    permission = permission_by_name(db, "app:read")

    listed = client.get(
        "/admin/permissions",
        headers=AUTH_HEADERS,
        params={"keyword": "APPLICATIONS", "resource": "app"},
    )
    assert listed.status_code == 200
    listed_body = listed.json()
    assert listed_body["total"] == 1
    assert listed_body["items"][0]["resource"] == "app"
    assert listed_body["items"][0]["action"] == "read"
    assert listed_body["items"][0]["endpoint_count"] == 1
    assert listed_body["items"][0]["role_count"] == 1
    assert permission_calls[-1] == ("permission:read",)

    detail = client.get(f"/admin/permissions/{permission.id}", headers=AUTH_HEADERS)
    assert detail.status_code == 200
    assert detail.json()["endpoints"] == [
        {
            "http_method": "GET",
            "path": "/admin/apps",
            "route_name": "list_apps",
        }
    ]

    updated = client.patch(
        f"/admin/permissions/{permission.id}",
        headers={**AUTH_HEADERS, "X-Request-ID": "req-permission-update-api"},
        json={
            "display_name": "  Applications  ",
            "description": "  Read application data  ",
            "version": 1,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["changed"] is True
    assert updated.json()["display_name"] == "Applications"
    assert updated.json()["version"] == 2
    assert permission_calls[-1] == ("permission:update",)

    disabled = client.post(
        f"/admin/permissions/{permission.id}/disable",
        headers={**AUTH_HEADERS, "X-Request-ID": "req-permission-disable-api"},
        json={"reason": "maintenance"},
    )
    assert disabled.status_code == 200
    assert disabled.json()["changed"] is True
    assert disabled.json()["is_enabled"] is False
    assert disabled.json()["revoked_sessions"] == 1
    assert permission_calls[-1] == ("permission:disable",)
    assert "revoked" in redis.values.values()

    repeated_disable = client.post(
        f"/admin/permissions/{permission.id}/disable",
        headers=AUTH_HEADERS,
        json={"reason": "replace"},
    )
    assert repeated_disable.status_code == 200
    assert repeated_disable.json()["changed"] is False
    assert repeated_disable.json()["disabled_reason"] == "maintenance"
    assert repeated_disable.json()["version"] == 3

    enabled = client.post(
        f"/admin/permissions/{permission.id}/enable",
        headers=AUTH_HEADERS,
    )
    assert enabled.status_code == 200
    assert enabled.json()["changed"] is True
    assert enabled.json()["is_enabled"] is True
    assert enabled.json()["disabled_at"] is None
    assert enabled.json()["version"] == 4
    assert permission_calls[-1] == ("permission:enable",)

    audits = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.target_type == "permission")
        .order_by(AuditEvent.id)
    ).all()
    assert [audit.action for audit in audits] == [
        "permission.updated",
        "permission.disabled",
        "permission.enabled",
    ]
    assert audits[0].request_id == "req-permission-update-api"
    assert audits[1].request_id == "req-permission-disable-api"
    audit_text = str([audit.changes_json for audit in audits]).lower()
    assert "token" not in audit_text
    assert "sid-target" not in audit_text


def test_permission_api_maps_domain_and_validation_errors(api_context) -> None:
    client, db, _permission_calls, _auth_state, _redis = api_context
    permission = permission_by_name(db, "app:read")
    core = permission_by_name(db, "permission:enable")

    missing = client.get("/admin/permissions/999", headers=AUTH_HEADERS)
    stale = client.patch(
        f"/admin/permissions/{permission.id}",
        headers=AUTH_HEADERS,
        json={"display_name": "stale", "version": 999},
    )
    protected = client.post(
        f"/admin/permissions/{core.id}/disable",
        headers=AUTH_HEADERS,
        json={},
    )
    invalid = client.patch(
        f"/admin/permissions/{permission.id}",
        headers=AUTH_HEADERS,
        json={"display_name": "   ", "version": 1},
    )
    unsupported_create = client.post(
        "/admin/permissions",
        headers=AUTH_HEADERS,
        json={"name": "new:read"},
    )
    unsupported_delete = client.delete(f"/admin/permissions/{permission.id}", headers=AUTH_HEADERS)

    assert missing.status_code == 404
    assert missing.json() == {"detail": "PERMISSION_NOT_FOUND"}
    assert stale.status_code == 409
    assert stale.json() == {"detail": "PERMISSION_VERSION_CONFLICT"}
    assert protected.status_code == 409
    assert protected.json() == {"detail": "PROTECTED_PERMISSION_OPERATION"}
    assert invalid.status_code == 422
    assert unsupported_create.status_code == 405
    assert unsupported_delete.status_code == 405


def test_permission_api_rolls_back_business_data_and_audit_on_domain_failure(
    api_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, db, _permission_calls, _auth_state, _redis = api_context
    permission = permission_by_name(db, "app:read")

    def fail_after_flush(
        service: Any,
        permission_id: int,
        *,
        actor_user_id: int,
        reason: str | None = None,
        request_id: str | None = None,
    ) -> tuple[Any, bool, int]:
        target = service.db.get(Permission, permission_id)
        assert target is not None
        target.is_enabled = False
        service.db.add(
            AuditEvent(
                actor_user_id=actor_user_id,
                action="permission.disabled",
                target_type="permission",
                target_id=target.id,
                result="success",
                changes_json={},
                request_id=request_id or "unknown",
            )
        )
        service.db.flush()
        raise PermissionDeniedError("forced failure")

    monkeypatch.setattr(
        "app.api.admin_permissions.AdminPermissionService.disable_permission",
        fail_after_flush,
    )
    response = client.post(
        f"/admin/permissions/{permission.id}/disable",
        headers={**AUTH_HEADERS, "X-Request-ID": "req-permission-rollback"},
        json={},
    )

    assert response.status_code == 500
    db.expire_all()
    restored = db.get(Permission, permission.id)
    assert restored is not None
    assert restored.is_enabled is True
    assert db.scalars(select(AuditEvent)).all() == []
