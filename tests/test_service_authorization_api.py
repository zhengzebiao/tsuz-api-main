from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import dependencies
from app.api.admin_resource_scopes import router as resource_scopes_router
from app.api.admin_service_grants import router as service_grants_router
from app.core.database import Base, get_db
from app.core.logging import RequestIdMiddleware
from app.models.app import App
from app.models.audit_event import AuditEvent
from app.models.user import User
from app.services.authorization_service import AuthenticationError, PermissionDeniedError

AUTH_HEADERS = {"Authorization": "Bearer access"}


@pytest.fixture
def api_context(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, DbSession, App, App, list[tuple[str, ...]], dict[str, bool]]]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    actor = User(email="admin@example.com", hashed_password="hash")
    caller = App(
        app_id="app_caller",
        app_secret_hash="a" * 64,
        name="Caller",
        access_url="https://caller.example.com",
        service_account_name="Caller Service",
    )
    target = App(
        app_id="app_target",
        app_secret_hash="b" * 64,
        name="Target",
        access_url="https://target.example.com",
        service_account_name="Target Service",
    )
    db.add_all([actor, caller, target])
    db.commit()
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
            current = self.db.get(User, actor_id)
            assert current is not None
            return current

    monkeypatch.setattr(dependencies, "AuthorizationService", AuthorizationStub)

    def override_db() -> Iterator[DbSession]:
        yield db

    application = FastAPI()
    application.add_middleware(RequestIdMiddleware)
    application.include_router(resource_scopes_router)
    application.include_router(service_grants_router)
    application.dependency_overrides[get_db] = override_db
    try:
        with TestClient(application) as client:
            yield client, db, caller, target, permission_calls, auth_state
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_admin_service_authorization_requires_user_permissions(api_context) -> None:
    client, _db, _caller, _target, permission_calls, auth_state = api_context

    assert client.get("/admin/resource-scopes").status_code == 401
    auth_state["allowed"] = False
    assert client.get("/admin/service-grants", headers=AUTH_HEADERS).status_code == 403
    auth_state["allowed"] = True
    assert client.get("/admin/resource-scopes", headers=AUTH_HEADERS).status_code == 200
    assert permission_calls[-1] == ("resource_scope:read",)


def test_scope_and_grant_http_lifecycle_is_audited(api_context) -> None:
    client, db, caller, target, permission_calls, _auth_state = api_context

    created_scope = client.post(
        "/admin/resource-scopes",
        headers={**AUTH_HEADERS, "X-Request-ID": "scope-create"},
        json={
            "target_app_id": target.app_id,
            "scope_code": "target:record:read",
            "description": "Read target records",
        },
    )
    assert created_scope.status_code == 201
    scope = created_scope.json()
    assert scope["target_app_id"] == target.app_id
    assert permission_calls[-1] == ("resource_scope:create",)

    duplicate = client.post(
        "/admin/resource-scopes",
        headers=AUTH_HEADERS,
        json={
            "target_app_id": target.app_id,
            "scope_code": "target:record:read",
        },
    )
    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "RESOURCE_SCOPE_ALREADY_EXISTS"}

    disabled = client.post(
        f"/admin/resource-scopes/{scope['id']}/disable",
        headers=AUTH_HEADERS,
    )
    repeated_disable = client.post(
        f"/admin/resource-scopes/{scope['id']}/disable",
        headers=AUTH_HEADERS,
    )
    enabled = client.post(
        f"/admin/resource-scopes/{scope['id']}/enable",
        headers=AUTH_HEADERS,
    )
    assert disabled.json()["changed"] is True
    assert repeated_disable.json()["changed"] is False
    assert enabled.json()["changed"] is True

    created_grant = client.post(
        "/admin/service-grants",
        headers={**AUTH_HEADERS, "X-Request-ID": "grant-create"},
        json={"caller_app_id": caller.app_id, "scope_id": scope["id"], "expires_at": None},
    )
    assert created_grant.status_code == 201
    grant = created_grant.json()
    assert grant["target_app_id"] == target.app_id
    assert grant["scope_code"] == "target:record:read"
    assert grant["changed"] is True
    assert permission_calls[-1] == ("service_grant:create",)

    repeated = client.post(
        "/admin/service-grants",
        headers=AUTH_HEADERS,
        json={"caller_app_id": caller.app_id, "scope_id": scope["id"], "expires_at": None},
    )
    assert repeated.status_code == 201
    assert repeated.json()["id"] == grant["id"]
    assert repeated.json()["changed"] is False

    revoked = client.post(
        f"/admin/service-grants/{grant['id']}/revoke",
        headers={**AUTH_HEADERS, "X-Request-ID": "grant-revoke"},
        json={"reason": "retired"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["changed"] is True
    assert permission_calls[-1] == ("service_grant:revoke",)

    recreate = client.post(
        "/admin/service-grants",
        headers=AUTH_HEADERS,
        json={"caller_app_id": caller.app_id, "scope_id": scope["id"], "expires_at": None},
    )
    assert recreate.status_code == 409
    assert recreate.json() == {"detail": "APP_SERVICE_GRANT_REVOKED"}

    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 5
    audits = db.scalars(select(AuditEvent).order_by(AuditEvent.id)).all()
    assert audits[0].request_id == "scope-create"
    assert audits[-1].request_id == "grant-revoke"
    audit_text = str([event.changes_json for event in audits])
    assert "secret" not in audit_text.lower()
    assert "token" not in audit_text.lower()


def test_admin_service_authorization_validates_targets_and_scope_shape(api_context) -> None:
    client, _db, _caller, _target, _permission_calls, _auth_state = api_context

    missing_target = client.post(
        "/admin/resource-scopes",
        headers=AUTH_HEADERS,
        json={"target_app_id": "missing", "scope_code": "target:record:read"},
    )
    malformed_scope = client.post(
        "/admin/resource-scopes",
        headers=AUTH_HEADERS,
        json={"target_app_id": "missing", "scope_code": "user:read"},
    )
    missing_caller = client.post(
        "/admin/service-grants",
        headers=AUTH_HEADERS,
        json={"caller_app_id": "missing", "scope_id": 999},
    )

    assert missing_target.status_code == 404
    assert missing_target.json() == {"detail": "RESOURCE_SCOPE_TARGET_NOT_FOUND"}
    assert malformed_scope.status_code == 422
    assert missing_caller.status_code == 404
    assert missing_caller.json() == {"detail": "APP_SERVICE_GRANT_CALLER_NOT_FOUND"}
