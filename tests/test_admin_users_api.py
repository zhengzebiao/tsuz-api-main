from collections.abc import Iterator
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.session_service as session_module
from app.core.database import Base, get_db
from app.main import app
from app.models.audit_event import AuditEvent
from app.models.session import Session as AuthSession
from app.models.user import User


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value

    def get(self, key: str) -> str | None:
        return self.values.get(key)


@pytest.fixture
def api_context(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, DbSession, User]]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = TestingSessionLocal()
    actor = User(email="admin@example.com", hashed_password="hashed-password", is_active=True)
    target = User(email="target@example.com", display_name="Target", hashed_password="hashed-password")
    db.add_all([actor, target])
    db.commit()
    db.refresh(actor)
    actor_id = actor.id
    monkeypatch.setattr(session_module, "get_redis", lambda: FakeRedis())

    def override_db() -> Iterator[DbSession]:
        yield db

    app.dependency_overrides[get_db] = override_db
    original_overrides = dict(app.dependency_overrides)
    for included in app.routes:
        router = getattr(included, "original_router", None)
        if router is None:
            continue
        for route in router.routes:
            for dependant in getattr(route, "dependant", ()).dependencies:
                call = dependant.call
                if getattr(call, "__name__", "") == "dependency":
                    app.dependency_overrides[call] = lambda actor_id=actor_id: db.get(User, actor_id)

    try:
        with TestClient(app) as client:
            yield client, db, actor
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original_overrides)
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def target_user(db: DbSession) -> User:
    user = db.scalar(select(User).where(User.email == "target@example.com"))
    assert user is not None
    return user


def test_admin_routes_are_registered_with_expected_methods() -> None:
    paths = app.openapi()["paths"]

    assert set(paths["/admin/users"]) >= {"get", "post"}
    assert set(paths["/admin/users/{user_id}"]) >= {"get", "patch"}
    for action in ("disable", "enable", "blacklist", "recover", "reset-password", "force-logout"):
        assert "post" in paths[f"/admin/users/{{user_id}}/{action}"]


def test_list_and_detail_do_not_expose_password(api_context) -> None:
    client, db, _actor = api_context
    target = target_user(db)

    list_response = client.get("/admin/users", params={"keyword": "target", "page_size": 1})
    detail_response = client.get(f"/admin/users/{target.id}")

    assert list_response.status_code == 200
    assert list_response.json()["total"] == 1
    assert list_response.json()["items"][0]["email"] == "target@example.com"
    assert detail_response.status_code == 200
    assert detail_response.json()["display_name"] == "Target"
    assert "hashed_password" not in str(list_response.json())
    assert "hashed_password" not in detail_response.json()


def test_admin_responses_serialize_qq_only_user_with_null_email(api_context) -> None:
    client, db, _actor = api_context
    qq_only = User(email=None, display_name="QQ Only", hashed_password=None, is_active=True)
    db.add(qq_only)
    db.commit()

    list_response = client.get("/admin/users", params={"keyword": "qq only"})
    detail_response = client.get(f"/admin/users/{qq_only.id}")

    assert list_response.status_code == 200
    assert list_response.json()["total"] == 1
    assert list_response.json()["items"][0]["email"] is None
    assert detail_response.status_code == 200
    assert detail_response.json()["email"] is None


def test_create_user_returns_201_and_rejects_sensitive_fields(api_context) -> None:
    client, _db, _actor = api_context

    response = client.post(
        "/admin/users",
        headers={"X-Request-ID": "req-create-api"},
        json={
            "email": "Created@Example.com",
            "display_name": "Created",
            "password": "created-password",
        },
    )
    rejected = client.post(
        "/admin/users",
        json={
            "email": "blocked@example.com",
            "password": "created-password",
            "hashed_password": "forbidden",
            "roles": ["admin"],
        },
    )

    assert response.status_code == 201
    assert response.json()["email"] == "created@example.com"
    assert "password" not in response.json()
    assert rejected.status_code == 422


def test_admin_api_maps_domain_errors(api_context) -> None:
    client, db, _actor = api_context
    target = target_user(db)

    weak_password = client.post(
        f"/admin/users/{target.id}/reset-password",
        json={"new_password": "short"},
    )
    missing = client.get("/admin/users/999")
    stale = client.patch(
        f"/admin/users/{target.id}",
        json={"display_name": "New", "version": 999},
    )

    assert weak_password.status_code == 400
    assert weak_password.json()["detail"] == "INVALID_PASSWORD"
    assert missing.status_code == 404
    assert missing.json()["detail"] == "USER_NOT_FOUND"
    assert stale.status_code == 409
    assert stale.json()["detail"] == "USER_VERSION_CONFLICT"


def test_qq_only_user_password_reset_returns_409_without_mutation(api_context) -> None:
    client, db, _actor = api_context
    target = User(email=None, hashed_password=None, is_active=True)
    db.add(target)
    db.flush()
    db.add(AuthSession(sid="sid-qq-only-api", user_id=target.id, status="active"))
    db.commit()
    original_version = target.version

    response = client.post(
        f"/admin/users/{target.id}/reset-password",
        json={"new_password": "replacement-password"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "PASSWORD_RESET_UNAVAILABLE"}
    db.refresh(target)
    assert target.hashed_password is None
    assert target.version == original_version
    auth_session = db.scalar(select(AuthSession).where(AuthSession.sid == "sid-qq-only-api"))
    assert auth_session is not None
    assert auth_session.status == "active"
    assert db.scalar(
        select(AuditEvent).where(
            AuditEvent.target_id == target.id,
            AuditEvent.action == "user.password_reset",
        )
    ) is None


def test_state_password_and_logout_endpoints_return_expected_shapes(api_context) -> None:
    client, db, _actor = api_context
    target = target_user(db)

    disabled = client.post(
        f"/admin/users/{target.id}/disable",
        headers={"X-Request-ID": "req-disable-api"},
        json={"reason": "left"},
    )
    enabled = client.post(f"/admin/users/{target.id}/enable")
    blacklisted = client.post(f"/admin/users/{target.id}/blacklist", json={"reason": "security"})
    recover = client.post(f"/admin/users/{target.id}/recover")
    reset = client.post(
        f"/admin/users/{target.id}/reset-password",
        json={"new_password": "replacement-password"},
    )
    logout = client.post(f"/admin/users/{target.id}/force-logout", json={"reason": "review"})

    assert disabled.status_code == 200
    assert disabled.json()["changed"] is True
    assert disabled.json()["is_active"] is False
    assert enabled.json()["is_active"] is True
    assert blacklisted.json()["is_blacklisted"] is True
    assert recover.json()["is_blacklisted"] is False
    assert reset.json() == {"message": "password reset", "revoked_sessions": 0}
    assert logout.json() == {"message": "user logged out", "revoked_sessions": 0}
    audits = db.scalars(select(AuditEvent).where(AuditEvent.target_id == target.id)).all()
    assert {audit.action for audit in audits} >= {
        "user.disabled",
        "user.enabled",
        "user.blacklisted",
        "user.recovered",
        "user.password_reset",
        "user.force_logout",
    }
    disable_audit = next(audit for audit in audits if audit.action == "user.disabled")
    assert disable_audit.request_id == "req-disable-api"
    assert isinstance(disable_audit.created_at, datetime)


def test_update_rejects_status_and_password_fields(api_context) -> None:
    client, db, _actor = api_context
    target = target_user(db)

    response = client.patch(
        f"/admin/users/{target.id}",
        json={
            "display_name": "Changed",
            "version": target.version,
            "is_active": False,
            "hashed_password": "forbidden",
        },
    )

    assert response.status_code == 422
