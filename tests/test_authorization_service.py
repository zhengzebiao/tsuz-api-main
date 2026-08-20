from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.permission import Permission
from app.models.session import Session as AuthSession
from app.models.user import User
from app.services.authorization_service import AuthenticationError, AuthorizationService, PermissionDeniedError


class RecordingTokenService:
    def __init__(self, payload: dict | None = None, *, invalid: bool = False) -> None:
        self.payload = payload or {}
        self.invalid = invalid

    def verify_access_token(self, access_token: str) -> dict:
        assert access_token == "access-token"
        if self.invalid:
            raise ValueError("invalid token")
        return dict(self.payload)


class RecordingBlacklistService:
    def __init__(self, *, rejected: bool = False) -> None:
        self.rejected = rejected
        self.checked: list[str] = []

    def ensure_not_blacklisted(self, jti: str) -> None:
        self.checked.append(jti)
        if self.rejected:
            raise ValueError("token is blacklisted")


class RecordingSessionService:
    def __init__(self, *, rejected: bool = False) -> None:
        self.rejected = rejected
        self.checked: list[str] = []

    def ensure_session_active(self, sid: str) -> None:
        self.checked.append(sid)
        if self.rejected:
            raise ValueError("session is revoked")


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


def build_service(
    db: DbSession,
    user: User,
    *,
    scope: object = "user:read user:create",
    blacklisted_jti: bool = False,
    revoked_session: bool = False,
) -> AuthorizationService:
    service = AuthorizationService(db)
    service.tokens = RecordingTokenService(
        {"sub": str(user.id), "jti": "jti-admin", "sid": "sid-admin", "scope": scope}
    )
    service.blacklist = RecordingBlacklistService(rejected=blacklisted_jti)
    service.sessions = RecordingSessionService(rejected=revoked_session)
    return service


def create_user(db: DbSession, *, active: bool = True, blacklisted: bool = False) -> User:
    user = User(
        email="admin@example.com",
        hashed_password="hashed-password",
        is_active=active,
        is_blacklisted=blacklisted,
    )
    db.add(user)
    db.flush()
    db.add(AuthSession(sid="sid-admin", user_id=user.id, status="active"))
    db.commit()
    return user


def test_authorization_returns_user_when_access_token_has_permissions(db_session: DbSession) -> None:
    user = create_user(db_session)
    db_session.add_all(
        (
            Permission(name="user:read", display_name="user:read"),
            Permission(name="user:create", display_name="user:create"),
        )
    )
    db_session.commit()
    service = build_service(db_session, user)

    authenticated_user = service.require_permissions("access-token", ("user:read", "user:create"))

    assert authenticated_user.id == user.id
    assert service.blacklist.checked == ["jti-admin"]
    assert service.sessions.checked == ["sid-admin"]


@pytest.mark.parametrize(
    ("active", "blacklisted"),
    [(False, False), (True, True)],
)
def test_authorization_rejects_user_state(
    db_session: DbSession,
    active: bool,
    blacklisted: bool,
) -> None:
    user = create_user(db_session, active=active, blacklisted=blacklisted)
    service = build_service(db_session, user)

    with pytest.raises(AuthenticationError, match="invalid access token"):
        service.require_permissions("access-token", ("user:read",))


@pytest.mark.parametrize(
    ("blacklisted_jti", "revoked_session"),
    [(True, False), (False, True)],
)
def test_authorization_rejects_revoked_access_state(
    db_session: DbSession,
    blacklisted_jti: bool,
    revoked_session: bool,
) -> None:
    user = create_user(db_session)
    service = build_service(
        db_session,
        user,
        blacklisted_jti=blacklisted_jti,
        revoked_session=revoked_session,
    )

    with pytest.raises(AuthenticationError, match="invalid access token"):
        service.require_permissions("access-token", ("user:read",))


def test_authorization_rejects_missing_permission(db_session: DbSession) -> None:
    user = create_user(db_session)
    service = build_service(db_session, user, scope="user:read")

    with pytest.raises(PermissionDeniedError, match="insufficient permissions"):
        service.require_permissions("access-token", ("user:create",))


@pytest.mark.parametrize(
    ("declared", "enabled"),
    [(False, True), (True, False), (False, False)],
)
def test_authorization_rejects_inactive_database_permission(
    db_session: DbSession,
    declared: bool,
    enabled: bool,
) -> None:
    user = create_user(db_session)
    db_session.add(
        Permission(
            name="user:read",
            display_name="user:read",
            is_declared=declared,
            is_enabled=enabled,
        )
    )
    db_session.commit()
    service = build_service(db_session, user, scope="user:read")

    with pytest.raises(PermissionDeniedError, match="insufficient permissions"):
        service.require_permissions("access-token", ("user:read",))


def test_authorization_rejects_scope_permission_missing_from_database(
    db_session: DbSession,
) -> None:
    user = create_user(db_session)
    service = build_service(db_session, user, scope="user:read")

    with pytest.raises(PermissionDeniedError, match="insufficient permissions"):
        service.require_permissions("access-token", ("user:read",))


def test_authorization_rejects_mixed_active_and_inactive_required_permissions(
    db_session: DbSession,
) -> None:
    user = create_user(db_session)
    db_session.add_all(
        (
            Permission(name="user:read", display_name="user:read"),
            Permission(
                name="user:create",
                display_name="user:create",
                is_enabled=False,
            ),
        )
    )
    db_session.commit()
    service = build_service(db_session, user)

    with pytest.raises(PermissionDeniedError, match="insufficient permissions"):
        service.require_permissions("access-token", ("user:read", "user:create"))


def test_authorization_rejects_malformed_scope(db_session: DbSession) -> None:
    user = create_user(db_session)
    service = build_service(db_session, user, scope=["user:read"])

    with pytest.raises(AuthenticationError, match="invalid access token"):
        service.require_permissions("access-token", ("user:read",))
