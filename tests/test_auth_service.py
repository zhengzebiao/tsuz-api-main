import logging
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

import app.services.session_service as session_module
from app.core.database import Base
from app.core.security import hash_password
from app.models.permission import Permission
from app.models.role import Role, role_permissions, user_roles
from app.models.session import Session as AuthSession
from app.models.user import User
from app.schemas.auth import LoginRequest
from app.services.auth_service import AuthService
from app.services.authorization_service import AuthenticationError
from app.services.refresh_token_service import RefreshTokenReuseError


class RecordingTokenService:
    expires_in_seconds = 3600

    def __init__(self, payload: dict | None = None) -> None:
        self.payload = payload or {}
        self.created: list[dict[str, object]] = []

    def create_access_token(self, user_id: str, sid: str, roles: list[str], scope: str) -> str:
        self.created.append({"user_id": user_id, "sid": sid, "roles": roles, "scope": scope})
        return "access-token"

    def verify_access_token(self, token: str) -> dict:
        assert token == "access"
        return dict(self.payload)


class RecordingRefreshTokenService:
    def __init__(self, rotation: dict | None = None) -> None:
        self.rotation = rotation or {}
        self.created: list[dict[str, str]] = []
        self.revoked: list[str] = []
        self.session_checks: list[str] = []

    def create_refresh_token(self, user_id: str, sid: str) -> str:
        self.created.append({"user_id": user_id, "sid": sid})
        return "refresh-token"

    def rotate_refresh_token(self, refresh_token: str) -> dict:
        assert refresh_token == "refresh"
        return dict(self.rotation)

    def revoke_session(self, sid: str) -> None:
        self.revoked.append(sid)

    def ensure_session_active(self, sid: str) -> None:
        self.session_checks.append(sid)


class RecordingBlacklistService:
    def __init__(self) -> None:
        self.added: list[tuple[str, int]] = []
        self.checked: list[str] = []

    def add_jti(self, jti: str, exp: int) -> None:
        self.added.append((jti, exp))

    def ensure_not_blacklisted(self, jti: str) -> None:
        self.checked.append(jti)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value
        if ex is not None:
            self.expirations[key] = ex


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    redis = FakeRedis()
    monkeypatch.setattr(session_module, "get_redis", lambda: redis)
    return redis


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


def create_user_with_rbac(
    db: DbSession,
    *,
    active: bool = True,
    blacklisted: bool = False,
    password: str = "password123",
) -> User:
    user = User(
        email="admin@example.com",
        hashed_password=hash_password(password),
        is_active=active,
        is_blacklisted=blacklisted,
    )
    role = Role(name="admin")
    read_permission = Permission(name="user:read", description="Read current user")
    write_permission = Permission(name="user:write", description="Write current user")
    db.add_all([user, role, read_permission, write_permission])
    db.flush()
    db.execute(user_roles.insert().values(user_id=user.id, role_id=role.id))
    db.execute(role_permissions.insert().values(role_id=role.id, permission_id=read_permission.id))
    db.execute(role_permissions.insert().values(role_id=role.id, permission_id=write_permission.id))
    db.commit()
    return user


def attach_disabled_role_with_permission(db: DbSession, user: User) -> None:
    disabled_role = Role(name="archived-admin", is_enabled=False)
    disabled_permission = Permission(name="role:disable", description="Must not enter scope")
    db.add_all([disabled_role, disabled_permission])
    db.flush()
    db.execute(user_roles.insert().values(user_id=user.id, role_id=disabled_role.id))
    db.execute(
        role_permissions.insert().values(
            role_id=disabled_role.id,
            permission_id=disabled_permission.id,
        )
    )
    db.commit()


def attach_inactive_permissions_to_enabled_role(db: DbSession, user: User) -> None:
    role = Role(name="permission-state-reviewer")
    disabled = Permission(
        name="app:disable",
        description="Disabled permission",
        is_enabled=False,
    )
    missing = Permission(
        name="app:enable",
        description="Missing permission",
        is_declared=False,
    )
    db.add_all((role, disabled, missing))
    db.flush()
    db.execute(user_roles.insert().values(user_id=user.id, role_id=role.id))
    db.execute(
        role_permissions.insert().values(role_id=role.id, permission_id=disabled.id)
    )
    db.execute(
        role_permissions.insert().values(role_id=role.id, permission_id=missing.id)
    )
    db.commit()


def attach_service_fakes(service: AuthService, *, token_payload: dict | None = None, rotation: dict | None = None):
    tokens = RecordingTokenService(payload=token_payload)
    refresh_tokens = RecordingRefreshTokenService(rotation=rotation)
    blacklist = RecordingBlacklistService()
    service.tokens = tokens
    service.refresh_tokens = refresh_tokens
    service.blacklist = blacklist
    return tokens, refresh_tokens, blacklist


def test_login_uses_db_user_password_and_rbac_claims(db_session: DbSession) -> None:
    user = create_user_with_rbac(db_session)
    service = AuthService(db_session)
    tokens, refresh_tokens, _blacklist = attach_service_fakes(service)

    response = service.login(LoginRequest(username="admin@example.com", password="password123"))

    assert response.access_token == "access-token"
    assert response.refresh_token == "refresh-token"
    sid = tokens.created[0]["sid"]
    assert tokens.created == [
        {"user_id": str(user.id), "sid": sid, "roles": ["admin"], "scope": "user:read user:write"}
    ]
    assert refresh_tokens.created == [{"user_id": str(user.id), "sid": sid}]
    db_session.expire_all()
    auth_session = db_session.scalar(select(AuthSession).where(AuthSession.user_id == user.id))
    assert auth_session is not None
    assert auth_session.sid == sid
    assert auth_session.status == "active"


def test_login_by_email_normalizes_input_and_uses_shared_login_path(db_session: DbSession) -> None:
    user = create_user_with_rbac(db_session)
    service = AuthService(db_session)
    tokens, refresh_tokens, _blacklist = attach_service_fakes(service)

    response = service.login_by_email("  ADMIN@EXAMPLE.COM  ", "password123")

    assert response.access_token == "access-token"
    assert tokens.created[0]["user_id"] == str(user.id)
    assert refresh_tokens.created[0]["user_id"] == str(user.id)


def test_complete_login_rechecks_user_state_and_uses_shared_token_path(db_session: DbSession) -> None:
    user = create_user_with_rbac(db_session)
    service = AuthService(db_session)
    tokens, refresh_tokens, _blacklist = attach_service_fakes(service)

    response = service.complete_login(user.id)

    assert response.access_token == "access-token"
    assert response.refresh_token == "refresh-token"
    sid = tokens.created[0]["sid"]
    assert refresh_tokens.created == [{"user_id": str(user.id), "sid": sid}]

    user.is_active = False
    db_session.commit()
    with pytest.raises(ValueError, match="invalid user"):
        service.complete_login(user.id)


def test_login_excludes_disabled_roles_and_permissions_from_claims(db_session: DbSession) -> None:
    user = create_user_with_rbac(db_session)
    attach_disabled_role_with_permission(db_session, user)
    attach_inactive_permissions_to_enabled_role(db_session, user)
    service = AuthService(db_session)
    tokens, _refresh_tokens, _blacklist = attach_service_fakes(service)

    service.login(LoginRequest(username="admin@example.com", password="password123"))

    assert tokens.created[0]["roles"] == ["admin", "permission-state-reviewer"]
    assert tokens.created[0]["scope"] == "user:read user:write"


def test_login_reflects_role_permission_replacement(
    db_session: DbSession,
) -> None:
    create_user_with_rbac(db_session)
    role = db_session.scalar(select(Role).where(Role.name == "admin"))
    app_read = Permission(name="app:read", display_name="View apps")
    db_session.add(app_read)
    db_session.flush()
    assert role is not None
    existing_permission_ids = set(
        db_session.scalars(
            select(role_permissions.c.permission_id).where(
                role_permissions.c.role_id == role.id
            )
        ).all()
    )
    db_session.execute(
        role_permissions.delete().where(
            role_permissions.c.role_id == role.id,
            role_permissions.c.permission_id.in_(existing_permission_ids),
        )
    )
    db_session.execute(
        role_permissions.insert().values(
            role_id=role.id,
            permission_id=app_read.id,
        )
    )
    db_session.commit()

    service = AuthService(db_session)
    tokens, _refresh_tokens, _blacklist = attach_service_fakes(service)
    service.login(LoginRequest(username="admin@example.com", password="password123"))

    assert tokens.created[0]["scope"] == "app:read"


def test_login_rejects_wrong_password(db_session: DbSession) -> None:
    create_user_with_rbac(db_session)
    service = AuthService(db_session)
    attach_service_fakes(service)

    with pytest.raises(ValueError, match="invalid credentials"):
        service.login(LoginRequest(username="admin@example.com", password="wrong"))


def test_login_failure_logs_reason_without_password(db_session: DbSession, caplog) -> None:
    create_user_with_rbac(db_session)
    service = AuthService(db_session)
    attach_service_fakes(service)
    raw_password = "wrong-password-secret"

    with (
        caplog.at_level(logging.WARNING, logger="app.auth"),
        pytest.raises(ValueError, match="invalid credentials"),
    ):
        service.login(LoginRequest(username="admin@example.com", password=raw_password))

    assert "reason=invalid_credentials" in caplog.text
    assert "admin@example.com" not in caplog.text
    assert raw_password not in caplog.text


def test_login_rejects_inactive_user(db_session: DbSession) -> None:
    create_user_with_rbac(db_session, active=False)
    service = AuthService(db_session)
    attach_service_fakes(service)

    with pytest.raises(ValueError, match="invalid credentials"):
        service.login(LoginRequest(username="admin@example.com", password="password123"))


def test_login_rejects_blacklisted_user(db_session: DbSession) -> None:
    create_user_with_rbac(db_session, blacklisted=True)
    service = AuthService(db_session)
    attach_service_fakes(service)

    with pytest.raises(ValueError, match="invalid credentials"):
        service.login(LoginRequest(username="admin@example.com", password="password123"))


def test_refresh_uses_db_user_session_and_rbac_claims(db_session: DbSession) -> None:
    user = create_user_with_rbac(db_session)
    db_session.add(AuthSession(sid="sid-refresh", user_id=user.id, status="active"))
    db_session.commit()
    service = AuthService(db_session)
    tokens, _refresh_tokens, _blacklist = attach_service_fakes(
        service,
        rotation={"user_id": str(user.id), "sid": "sid-refresh", "refresh_token": "new-refresh-token"},
    )

    response = service.refresh("refresh")

    assert response.access_token == "access-token"
    assert response.refresh_token == "new-refresh-token"
    assert tokens.created == [
        {"user_id": str(user.id), "sid": "sid-refresh", "roles": ["admin"], "scope": "user:read user:write"}
    ]


def test_refresh_excludes_disabled_roles_and_permissions_from_claims(db_session: DbSession) -> None:
    user = create_user_with_rbac(db_session)
    attach_disabled_role_with_permission(db_session, user)
    attach_inactive_permissions_to_enabled_role(db_session, user)
    db_session.add(AuthSession(sid="sid-filtered-refresh", user_id=user.id, status="active"))
    db_session.commit()
    service = AuthService(db_session)
    tokens, _refresh_tokens, _blacklist = attach_service_fakes(
        service,
        rotation={
            "user_id": str(user.id),
            "sid": "sid-filtered-refresh",
            "refresh_token": "new-refresh-token",
        },
    )

    service.refresh("refresh")

    assert tokens.created[0]["roles"] == ["admin", "permission-state-reviewer"]
    assert tokens.created[0]["scope"] == "user:read user:write"


def test_refresh_rejects_blacklisted_user_and_revokes_session(db_session: DbSession) -> None:
    user = create_user_with_rbac(db_session, blacklisted=True)
    db_session.add(AuthSession(sid="sid-blacklisted", user_id=user.id, status="active"))
    db_session.commit()
    service = AuthService(db_session)
    attach_service_fakes(
        service,
        rotation={"user_id": str(user.id), "sid": "sid-blacklisted", "refresh_token": "new-refresh-token"},
    )

    with pytest.raises(ValueError, match="invalid user"):
        service.refresh("refresh")

    db_session.expire_all()
    auth_session = db_session.scalar(select(AuthSession).where(AuthSession.sid == "sid-blacklisted"))
    assert auth_session is not None
    assert auth_session.status == "revoked"
    assert auth_session.revoked_reason == "authentication_state_changed"
    assert auth_session.revoked_at is not None


def test_refresh_reuse_revokes_db_session(db_session: DbSession) -> None:
    user = create_user_with_rbac(db_session)
    db_session.add(AuthSession(sid="sid-reuse", user_id=user.id, status="active"))
    db_session.commit()
    service = AuthService(db_session)
    tokens, _refresh_tokens, _blacklist = attach_service_fakes(service)

    class ReusedRefreshTokenService:
        def rotate_refresh_token(self, refresh_token: str) -> dict:
            assert refresh_token == "refresh"
            raise RefreshTokenReuseError("sid-reuse")

    service.refresh_tokens = ReusedRefreshTokenService()

    with pytest.raises(ValueError, match="reuse detected"):
        service.refresh("refresh")

    assert tokens.created == []
    db_session.expire_all()
    auth_session = db_session.scalar(select(AuthSession).where(AuthSession.sid == "sid-reuse"))
    assert auth_session is not None
    assert auth_session.status == "revoked"
    assert auth_session.revoked_reason == "refresh_token_reuse"
    assert auth_session.revoked_at is not None


def test_current_user_returns_db_email_and_roles(db_session: DbSession) -> None:
    user = create_user_with_rbac(db_session)
    db_session.add(AuthSession(sid="sid-current", user_id=user.id, status="active"))
    db_session.commit()
    service = AuthService(db_session)
    token_payload = {"sub": str(user.id), "jti": "jti-current", "sid": "sid-current", "exp": 4_102_444_800}
    _tokens, refresh_tokens, blacklist = attach_service_fakes(service, token_payload=token_payload)

    response = service.current_user("access")

    assert response.id == str(user.id)
    assert response.username == "admin@example.com"
    assert response.roles == ["admin"]
    assert blacklist.checked == ["jti-current"]
    assert refresh_tokens.session_checks == []


def test_current_user_excludes_disabled_roles(db_session: DbSession) -> None:
    user = create_user_with_rbac(db_session)
    attach_disabled_role_with_permission(db_session, user)
    db_session.add(AuthSession(sid="sid-filtered-current", user_id=user.id, status="active"))
    db_session.commit()
    service = AuthService(db_session)
    token_payload = {
        "sub": str(user.id),
        "jti": "jti-filtered-current",
        "sid": "sid-filtered-current",
        "exp": 4_102_444_800,
    }
    attach_service_fakes(service, token_payload=token_payload)

    response = service.current_user("access")

    assert response.roles == ["admin"]


def test_current_user_rejects_blacklisted_user(db_session: DbSession) -> None:
    user = create_user_with_rbac(db_session, blacklisted=True)
    db_session.add(AuthSession(sid="sid-current", user_id=user.id, status="active"))
    db_session.commit()
    service = AuthService(db_session)
    token_payload = {"sub": str(user.id), "jti": "jti-current", "sid": "sid-current", "exp": 4_102_444_800}
    attach_service_fakes(service, token_payload=token_payload)

    with pytest.raises(AuthenticationError, match="invalid user"):
        service.current_user("access")


def test_current_user_rejects_revoked_db_session(db_session: DbSession) -> None:
    user = create_user_with_rbac(db_session)
    db_session.add(AuthSession(sid="sid-current", user_id=user.id, status="revoked"))
    db_session.commit()
    service = AuthService(db_session)
    token_payload = {"sub": str(user.id), "jti": "jti-current", "sid": "sid-current", "exp": 4_102_444_800}
    attach_service_fakes(service, token_payload=token_payload)

    with pytest.raises(ValueError, match="session is revoked"):
        service.current_user("access")


def test_logout_blacklists_jti_revokes_redis_and_db_session(db_session: DbSession) -> None:
    user = create_user_with_rbac(db_session)
    db_session.add(AuthSession(sid="sid-logout", user_id=user.id, status="active"))
    db_session.commit()
    service = AuthService(db_session)
    token_payload = {"sub": str(user.id), "jti": "jti-logout", "sid": "sid-logout", "exp": 4_102_444_800}
    _tokens, refresh_tokens, blacklist = attach_service_fakes(service, token_payload=token_payload)

    response = service.logout("access")

    assert response.message == "logged out"
    assert blacklist.added == [("jti-logout", 4_102_444_800)]
    assert refresh_tokens.revoked == []
    db_session.expire_all()
    auth_session = db_session.scalar(select(AuthSession).where(AuthSession.sid == "sid-logout"))
    assert auth_session is not None
    assert auth_session.status == "revoked"
    assert auth_session.revoked_reason == "user_logout"
    assert auth_session.revoked_at is not None


def test_logout_logs_without_raw_access_token(db_session: DbSession, caplog) -> None:
    user = create_user_with_rbac(db_session)
    db_session.add(AuthSession(sid="sid-logout", user_id=user.id, status="active"))
    db_session.commit()
    service = AuthService(db_session)
    token_payload = {"sub": str(user.id), "jti": "jti-logout", "sid": "sid-logout", "exp": 4_102_444_800}
    attach_service_fakes(service, token_payload=token_payload)
    raw_access_token = "access"

    with caplog.at_level(logging.INFO, logger="app.auth"):
        service.logout(raw_access_token)

    assert "logout succeeded" in caplog.text
    assert "sid-logout" not in caplog.text
    assert "jti-logout" not in caplog.text
    assert raw_access_token not in caplog.text
