from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.admin_apps as api_module
from app.api import dependencies
from app.api.admin_apps import router as admin_apps_router
from app.core.database import Base, get_db
from app.core.logging import RequestIdMiddleware
from app.core.security import hash_app_secret, verify_app_secret
from app.main import app as main_app
from app.models.app import App
from app.models.audit_event import AuditEvent
from app.models.user import User
from app.services.admin_app_service import AppCreationError, AppSecretGenerationError
from app.services.authorization_service import AuthenticationError, PermissionDeniedError

AUTH_HEADERS = {"Authorization": "Bearer access"}


@pytest.fixture
def api_context(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, DbSession, list[tuple[str, ...]], dict[str, bool]]]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = TestingSessionLocal()
    actor = User(email="admin@example.com", hashed_password="hashed-password", is_active=True)
    db.add(actor)
    db.commit()
    db.refresh(actor)
    actor_id = actor.id
    permission_calls: list[tuple[str, ...]] = []
    auth_state = {"allowed": True}

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

    def override_db() -> Iterator[DbSession]:
        yield db

    test_app = FastAPI()
    test_app.add_middleware(RequestIdMiddleware)
    test_app.include_router(admin_apps_router)
    test_app.dependency_overrides[get_db] = override_db

    try:
        with TestClient(test_app) as client:
            yield client, db, permission_calls, auth_state
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def create_payload() -> dict[str, str]:
    return {
        "name": "Project Management",
        "icon_url": "https://static.example.com/project.png",
        "access_url": "https://project.example.com",
        "service_account_name": "Project Management Service",
    }


def assert_permission(permission_calls: list[tuple[str, ...]], expected: str) -> None:
    assert permission_calls[-1] == (expected,)


def assert_no_credentials(payload: Any) -> None:
    serialized = str(payload)
    assert "app_secret" not in serialized
    assert "app_secret_hash" not in serialized


def test_admin_app_routes_are_registered_with_expected_methods() -> None:
    paths = main_app.openapi()["paths"]

    assert set(paths["/admin/apps"]) >= {"get", "post"}
    assert set(paths["/admin/apps/{app_id}"]) >= {"get", "patch"}
    for action in ("disable", "enable", "regenerate-secret"):
        assert "post" in paths[f"/admin/apps/{{app_id}}/{action}"]


def test_admin_app_routes_enforce_authentication_and_authorization(api_context) -> None:
    client, _db, _permission_calls, auth_state = api_context

    unauthenticated = client.get("/admin/apps")
    auth_state["allowed"] = False
    forbidden = client.get("/admin/apps", headers=AUTH_HEADERS)

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["detail"] == "invalid access token"
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"] == "insufficient permissions"


def test_app_lifecycle_uses_expected_permissions_and_safe_responses(api_context) -> None:
    client, db, permission_calls, _auth_state = api_context

    created = client.post(
        "/admin/apps",
        headers={**AUTH_HEADERS, "X-Request-ID": "req-app-create-api"},
        json=create_payload(),
    )

    assert created.status_code == 201
    assert created.headers["Cache-Control"] == "no-store"
    assert_permission(permission_calls, "app:create")
    created_body = created.json()
    app_data = created_body["app"]
    app_secret = created_body["app_secret"]
    app_id = app_data["id"]
    assert app_secret.startswith("app_secret_")
    assert "app_secret" not in app_data
    assert "app_secret_hash" not in app_data
    stored = db.get(App, app_id)
    assert stored is not None
    assert stored.app_secret_hash != app_secret
    assert verify_app_secret(app_secret, stored.app_secret_hash)

    listed = client.get(
        "/admin/apps",
        headers=AUTH_HEADERS,
        params={"keyword": "PROJECT", "is_enabled": True, "page_size": 1},
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["id"] == app_id
    assert_no_credentials(listed.json())
    assert_permission(permission_calls, "app:read")

    detail = client.get(f"/admin/apps/{app_id}", headers=AUTH_HEADERS)
    assert detail.status_code == 200
    assert detail.json()["app_id"] == app_data["app_id"]
    assert_no_credentials(detail.json())
    assert_permission(permission_calls, "app:read")

    updated = client.patch(
        f"/admin/apps/{app_id}",
        headers={**AUTH_HEADERS, "X-Request-ID": "req-app-update-api"},
        json={"name": "Project Hub", "version": app_data["version"]},
    )
    assert updated.status_code == 200
    assert updated.json()["changed"] is True
    assert updated.json()["name"] == "Project Hub"
    assert updated.json()["version"] == 2
    assert updated.headers.get("Cache-Control") is None
    assert_no_credentials(updated.json())
    assert_permission(permission_calls, "app:update")

    disabled = client.post(
        f"/admin/apps/{app_id}/disable",
        headers={**AUTH_HEADERS, "X-Request-ID": "req-app-disable-api"},
        json={"reason": "maintenance"},
    )
    assert disabled.status_code == 200
    assert disabled.json()["changed"] is True
    assert disabled.json()["is_enabled"] is False
    assert disabled.json()["disabled_reason"] == "maintenance"
    assert_permission(permission_calls, "app:disable")
    first_disabled_at = disabled.json()["disabled_at"]

    repeated_disable = client.post(
        f"/admin/apps/{app_id}/disable",
        headers=AUTH_HEADERS,
        json={"reason": "must not replace original reason"},
    )
    assert repeated_disable.status_code == 200
    assert repeated_disable.json()["changed"] is False
    assert repeated_disable.json()["disabled_at"] == first_disabled_at
    assert repeated_disable.json()["disabled_reason"] == "maintenance"
    assert repeated_disable.json()["version"] == 3
    assert_permission(permission_calls, "app:disable")

    enabled = client.post(f"/admin/apps/{app_id}/enable", headers=AUTH_HEADERS)
    assert enabled.status_code == 200
    assert enabled.json()["changed"] is True
    assert enabled.json()["is_enabled"] is True
    assert enabled.json()["disabled_at"] is None
    assert enabled.json()["disabled_reason"] is None
    assert enabled.json()["version"] == 4
    assert_no_credentials(enabled.json())
    assert_permission(permission_calls, "app:enable")

    repeated_enable = client.post(f"/admin/apps/{app_id}/enable", headers=AUTH_HEADERS)
    assert repeated_enable.status_code == 200
    assert repeated_enable.json()["changed"] is False
    assert repeated_enable.json()["version"] == 4
    assert_permission(permission_calls, "app:enable")

    regenerated = client.post(
        f"/admin/apps/{app_id}/regenerate-secret",
        headers={**AUTH_HEADERS, "X-Request-ID": "req-app-secret-api"},
        json={"reason": "rotation"},
    )
    assert regenerated.status_code == 200
    assert regenerated.headers["Cache-Control"] == "no-store"
    assert_permission(permission_calls, "app:regenerate_secret")
    new_secret = regenerated.json()["app_secret"]
    assert new_secret != app_secret
    db.expire_all()
    stored = db.get(App, app_id)
    assert stored is not None
    assert verify_app_secret(app_secret, stored.app_secret_hash) is False
    assert verify_app_secret(new_secret, stored.app_secret_hash) is True

    detail_after_rotation = client.get(f"/admin/apps/{app_id}", headers=AUTH_HEADERS)
    assert detail_after_rotation.status_code == 200
    assert_no_credentials(detail_after_rotation.json())
    assert detail_after_rotation.json()["version"] == 5

    audits = db.scalars(select(AuditEvent).where(AuditEvent.target_type == "app").order_by(AuditEvent.id)).all()
    assert [audit.action for audit in audits] == [
        "app.created",
        "app.updated",
        "app.disabled",
        "app.enabled",
        "app.secret_regenerated",
    ]
    assert audits[0].request_id == "req-app-create-api"
    assert audits[-1].request_id == "req-app-secret-api"
    audit_text = str([(audit.changes_json, audit.reason) for audit in audits])
    assert app_secret not in audit_text
    assert new_secret not in audit_text
    assert stored.app_secret_hash not in audit_text
    assert "app_secret" not in audit_text


def test_app_api_maps_not_found_version_and_validation_errors(api_context) -> None:
    client, _db, _permission_calls, _auth_state = api_context
    created = client.post("/admin/apps", headers=AUTH_HEADERS, json=create_payload())
    app_id = created.json()["app"]["id"]

    missing = client.get("/admin/apps/999", headers=AUTH_HEADERS)
    stale = client.patch(
        f"/admin/apps/{app_id}",
        headers=AUTH_HEADERS,
        json={"name": "Stale", "version": 999},
    )
    forbidden_fields = client.patch(
        f"/admin/apps/{app_id}",
        headers=AUTH_HEADERS,
        json={"is_enabled": False, "app_secret": "forbidden", "version": 1},
    )
    invalid_url = client.post(
        "/admin/apps",
        headers=AUTH_HEADERS,
        json={**create_payload(), "access_url": "ftp://project.example.com"},
    )

    assert missing.status_code == 404
    assert missing.json()["detail"] == "APP_NOT_FOUND"
    assert stale.status_code == 409
    assert stale.json()["detail"] == "APP_VERSION_CONFLICT"
    assert forbidden_fields.status_code == 422
    assert invalid_url.status_code == 422


def test_creation_failure_returns_safe_error_and_rolls_back(api_context, monkeypatch: pytest.MonkeyPatch) -> None:
    client, db, _permission_calls, _auth_state = api_context

    def fail_after_flush(
        service: api_module.AdminAppService,
        _payload: Any,
        *,
        actor_user_id: int,
        request_id: str | None = None,
    ) -> tuple[App, str]:
        assert actor_user_id > 0
        assert request_id is not None
        candidate = App(
            app_id="app_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            app_secret_hash=hash_app_secret("must-not-persist"),
            name="Must Roll Back",
            icon_url=None,
            access_url="https://rollback.example.com",
            service_account_name="Rollback Service",
        )
        service.db.add(candidate)
        service.db.flush()
        raise AppCreationError(AppCreationError.code)

    monkeypatch.setattr(api_module.AdminAppService, "create_app", fail_after_flush)

    response = client.post(
        "/admin/apps",
        headers={**AUTH_HEADERS, "X-Request-ID": "req-failed-create"},
        json=create_payload(),
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "APP_CREATION_FAILED"}
    assert response.headers.get("Cache-Control") is None
    assert "must-not-persist" not in response.text
    assert db.scalar(select(func.count()).select_from(App)) == 0
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_secret_generation_failure_returns_safe_error_and_rolls_back(
    api_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, db, _permission_calls, _auth_state = api_context
    created = client.post("/admin/apps", headers=AUTH_HEADERS, json=create_payload())
    app_id = created.json()["app"]["id"]
    stored = db.get(App, app_id)
    assert stored is not None
    original_hash = stored.app_secret_hash

    def fail_after_flush(
        service: api_module.AdminAppService,
        target_id: int,
        *,
        actor_user_id: int,
        reason: str,
        request_id: str | None = None,
    ) -> tuple[App, str]:
        assert actor_user_id > 0
        assert reason == "rotation"
        assert request_id is not None
        target = service.db.get(App, target_id)
        assert target is not None
        target.app_secret_hash = hash_app_secret("must-not-become-active")
        service.db.flush()
        raise AppSecretGenerationError(AppSecretGenerationError.code)

    monkeypatch.setattr(api_module.AdminAppService, "regenerate_secret", fail_after_flush)

    response = client.post(
        f"/admin/apps/{app_id}/regenerate-secret",
        headers={**AUTH_HEADERS, "X-Request-ID": "req-failed-secret"},
        json={"reason": "rotation"},
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "APP_SECRET_GENERATION_FAILED"}
    assert response.headers.get("Cache-Control") is None
    assert "must-not-become-active" not in response.text
    db.expire_all()
    stored = db.get(App, app_id)
    assert stored is not None
    assert stored.app_secret_hash == original_hash
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 1
