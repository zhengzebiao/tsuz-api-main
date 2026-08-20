from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.admin_users as api_module
import app.services.session_service as session_module
from app.api import dependencies
from app.api.admin_users import router as admin_users_router
from app.core.database import Base, get_db
from app.core.logging import RequestIdMiddleware
from app.main import app as main_app
from app.models.audit_event import AuditEvent
from app.models.role import Role, user_roles
from app.models.session import Session as AuthSession
from app.models.user import User
from app.services.admin_user_service import UserVersionConflictError
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
) -> Iterator[
    tuple[
        TestClient,
        DbSession,
        User,
        User,
        Role,
        Role,
        Role,
        list[tuple[str, ...]],
        dict[str, bool],
        FakeRedis,
    ]
]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = TestingSessionLocal()
    actor = User(email="admin@example.com", hashed_password="actor-hash", is_active=True)
    target = User(
        email="target@example.com",
        display_name="Target User",
        hashed_password="target-hash",
        is_active=True,
    )
    admin = Role(name="admin", description="Core administration")
    auditor = Role(name="auditor", description="Audit access")
    disabled = Role(name="disabled", description="Disabled role", is_enabled=False)
    db.add_all([actor, target, admin, auditor, disabled])
    db.flush()
    db.execute(user_roles.insert().values(user_id=actor.id, role_id=admin.id))
    db.commit()
    db.refresh(actor)
    db.refresh(target)
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
    test_app.include_router(admin_users_router)
    test_app.dependency_overrides[get_db] = override_db

    try:
        with TestClient(test_app) as client:
            yield (
                client,
                db,
                actor,
                target,
                admin,
                auditor,
                disabled,
                permission_calls,
                auth_state,
                redis,
            )
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def assigned_role_ids(db: DbSession, user_id: int) -> set[int]:
    return set(db.scalars(select(user_roles.c.role_id).where(user_roles.c.user_id == user_id)).all())


def test_admin_user_role_routes_are_registered_with_security_and_expected_methods() -> None:
    paths = main_app.openapi()["paths"]
    path = paths["/admin/users/{user_id}/roles"]

    assert set(path) >= {"get", "put"}
    assert path["get"]["security"]
    assert path["put"]["security"]
    assert path["put"]["requestBody"]["content"]["application/json"]["schema"]
    assert path["get"]["responses"]["200"]["content"]["application/json"]["schema"]


def test_user_role_routes_enforce_authentication_authorization_and_permissions(api_context) -> None:
    (
        client,
        _db,
        _actor,
        target,
        _admin,
        auditor,
        _disabled,
        permission_calls,
        auth_state,
        _redis,
    ) = api_context

    unauthenticated = client.get(f"/admin/users/{target.id}/roles")
    auth_state["allowed"] = False
    forbidden = client.get(f"/admin/users/{target.id}/roles", headers=AUTH_HEADERS)

    assert unauthenticated.status_code == 401
    assert unauthenticated.json() == {"detail": "invalid access token"}
    assert forbidden.status_code == 403
    assert forbidden.json() == {"detail": "insufficient permissions"}

    auth_state["allowed"] = True
    queried = client.get(f"/admin/users/{target.id}/roles", headers=AUTH_HEADERS)
    assigned = client.put(
        f"/admin/users/{target.id}/roles",
        headers=AUTH_HEADERS,
        json={"role_ids": [auditor.id], "version": target.version},
    )

    assert queried.status_code == 200
    assert permission_calls[-2] == ("user:read",)
    assert assigned.status_code == 200
    assert permission_calls[-1] == ("user:assign_roles",)


def test_user_roles_query_and_full_replacement_are_safe_idempotent_and_audited(api_context) -> None:
    (
        client,
        db,
        _actor,
        target,
        _admin,
        auditor,
        disabled,
        permission_calls,
        _auth_state,
        redis,
    ) = api_context
    db.execute(user_roles.insert().values(user_id=target.id, role_id=disabled.id))
    db.commit()
    db.refresh(target)

    queried = client.get(f"/admin/users/{target.id}/roles", headers=AUTH_HEADERS)

    assert queried.status_code == 200
    assert queried.json() == {
        "user_id": target.id,
        "roles": [
            {
                "id": disabled.id,
                "name": "disabled",
                "description": "Disabled role",
                "is_enabled": False,
            }
        ],
        "version": 1,
        "changed": False,
        "revoked_sessions": 0,
    }
    assert permission_calls[-1] == ("user:read",)
    assert "hashed_password" not in str(queried.json())
    assert "permissions" not in str(queried.json())

    db.add(AuthSession(sid="sid-user-role-api", user_id=target.id, status="active"))
    db.commit()
    assigned = client.put(
        f"/admin/users/{target.id}/roles",
        headers={**AUTH_HEADERS, "X-Request-ID": "req-user-roles-api"},
        json={"role_ids": [disabled.id, auditor.id], "version": 1},
    )

    assert assigned.status_code == 200
    assigned_body = assigned.json()
    assert assigned_body["changed"] is True
    assert assigned_body["revoked_sessions"] == 1
    assert assigned_body["version"] == 2
    assert [role["name"] for role in assigned_body["roles"]] == ["auditor", "disabled"]
    assert assigned_role_ids(db, target.id) == {auditor.id, disabled.id}
    assert permission_calls[-1] == ("user:assign_roles",)
    assert "revoked" in redis.values.values()

    repeated = client.put(
        f"/admin/users/{target.id}/roles",
        headers=AUTH_HEADERS,
        json={"role_ids": [auditor.id, disabled.id], "version": assigned_body["version"]},
    )
    assert repeated.status_code == 200
    assert repeated.json()["changed"] is False
    assert repeated.json()["version"] == 2
    assert repeated.json()["revoked_sessions"] == 0

    cleared = client.put(
        f"/admin/users/{target.id}/roles",
        headers=AUTH_HEADERS,
        json={"role_ids": [], "version": repeated.json()["version"]},
    )
    assert cleared.status_code == 200
    assert cleared.json()["changed"] is True
    assert cleared.json()["roles"] == []
    assert cleared.json()["version"] == 3
    assert assigned_role_ids(db, target.id) == set()

    audits = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.action == "user.roles_assigned", AuditEvent.target_id == target.id)
        .order_by(AuditEvent.id)
    ).all()
    assert len(audits) == 2
    assert audits[0].request_id == "req-user-roles-api"
    assert audits[0].changes_json["roles"]["from"] == [{"id": disabled.id, "name": "disabled"}]
    assert audits[0].changes_json["roles"]["to"] == [
        {"id": auditor.id, "name": "auditor"},
        {"id": disabled.id, "name": "disabled"},
    ]
    audit_text = str([audit.changes_json for audit in audits]).lower()
    assert "password" not in audit_text
    assert "permission" not in audit_text
    assert "sid-user-role-api" not in audit_text


def test_user_role_api_maps_not_found_disabled_version_and_validation_errors(api_context) -> None:
    (
        client,
        db,
        _actor,
        target,
        _admin,
        auditor,
        disabled,
        _permission_calls,
        _auth_state,
        _redis,
    ) = api_context

    missing_user = client.get("/admin/users/999/roles", headers=AUTH_HEADERS)
    missing_role = client.put(
        f"/admin/users/{target.id}/roles",
        headers=AUTH_HEADERS,
        json={"role_ids": [999], "version": target.version},
    )
    disabled_role = client.put(
        f"/admin/users/{target.id}/roles",
        headers=AUTH_HEADERS,
        json={"role_ids": [disabled.id], "version": target.version},
    )
    stale = client.put(
        f"/admin/users/{target.id}/roles",
        headers=AUTH_HEADERS,
        json={"role_ids": [auditor.id], "version": 999},
    )
    duplicate_ids = client.put(
        f"/admin/users/{target.id}/roles",
        headers=AUTH_HEADERS,
        json={"role_ids": [auditor.id, auditor.id], "version": target.version},
    )

    assert missing_user.status_code == 404
    assert missing_user.json() == {"detail": "USER_NOT_FOUND"}
    assert missing_role.status_code == 404
    assert missing_role.json() == {"detail": "ROLE_NOT_FOUND"}
    assert disabled_role.status_code == 409
    assert disabled_role.json() == {"detail": "ROLE_DISABLED"}
    assert stale.status_code == 409
    assert stale.json() == {"detail": "USER_VERSION_CONFLICT"}
    assert duplicate_ids.status_code == 422
    db.expire_all()
    assert assigned_role_ids(db, target.id) == set()
    assert db.get(User, target.id).version == 1
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_user_role_api_maps_admin_protection_errors(api_context) -> None:
    (
        client,
        db,
        actor,
        target,
        admin,
        _auditor,
        _disabled,
        _permission_calls,
        _auth_state,
        _redis,
    ) = api_context

    self_removal = client.put(
        f"/admin/users/{actor.id}/roles",
        headers=AUTH_HEADERS,
        json={"role_ids": [], "version": actor.version},
    )
    assert self_removal.status_code == 409
    assert self_removal.json() == {"detail": "SELF_OPERATION_NOT_ALLOWED"}

    db.execute(user_roles.insert().values(user_id=target.id, role_id=admin.id))
    db.commit()
    db.refresh(target)
    db.execute(
        user_roles.delete().where(
            user_roles.c.user_id == actor.id,
            user_roles.c.role_id == admin.id,
        )
    )
    db.commit()

    last_admin = client.put(
        f"/admin/users/{target.id}/roles",
        headers=AUTH_HEADERS,
        json={"role_ids": [], "version": target.version},
    )
    assert last_admin.status_code == 409
    assert last_admin.json() == {"detail": "LAST_ACTIVE_ADMIN"}
    assert assigned_role_ids(db, target.id) == {admin.id}


def test_user_role_write_failure_rolls_back_associations_version_and_audit(
    api_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        client,
        db,
        _actor,
        target,
        _admin,
        auditor,
        _disabled,
        _permission_calls,
        _auth_state,
        _redis,
    ) = api_context

    def fail_after_flush(
        service: api_module.AdminUserService,
        user_id: int,
        role_ids: list[int],
        version: int,
        *,
        actor_user_id: int,
        request_id: str | None = None,
    ) -> tuple[User, list[Role], bool, int]:
        user = service.db.get(User, user_id)
        assert user is not None
        role = service.db.get(Role, role_ids[0])
        assert role is not None
        service.db.execute(user_roles.insert().values(user_id=user.id, role_id=role.id))
        user.version = version + 1
        service.db.add(
            AuditEvent(
                actor_user_id=actor_user_id,
                action="user.roles_assigned",
                target_type="user",
                target_id=user.id,
                result="success",
                changes_json={},
                request_id=request_id or "unknown",
            )
        )
        service.db.flush()
        raise UserVersionConflictError(UserVersionConflictError.code)

    monkeypatch.setattr(api_module.AdminUserService, "assign_roles", fail_after_flush)

    response = client.put(
        f"/admin/users/{target.id}/roles",
        headers={**AUTH_HEADERS, "X-Request-ID": "req-user-role-rollback"},
        json={"role_ids": [auditor.id], "version": target.version},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "USER_VERSION_CONFLICT"}
    db.expire_all()
    assert assigned_role_ids(db, target.id) == set()
    assert db.get(User, target.id).version == 1
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0
