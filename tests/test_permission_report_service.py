from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as DbSession, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.app import App
from app.models.permission import Permission
from app.models.role import Role, role_permissions, user_roles
from app.models.user import User
from app.schemas.internal_permission_report import PermissionReportItem
from app.services.permission_report_service import PermissionReportNameConflictError, PermissionReportService


@pytest.fixture
def db_session() -> Iterator[DbSession]:
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def make_app(app_id: str) -> App:
    return App(
        app_id=app_id,
        app_secret_hash="a" * 64,
        name=app_id,
        access_url=f"https://{app_id}.example.com",
        service_account_name=f"{app_id} service",
    )


def test_report_creates_admin_grants_and_is_idempotent(db_session: DbSession, monkeypatch: pytest.MonkeyPatch) -> None:
    caller = make_app("app_jcc")
    role = Role(name="admin")
    user = User(email="admin@example.com", hashed_password="hash")
    db_session.add_all([caller, role, user])
    db_session.flush()
    db_session.execute(user_roles.insert().values(user_id=user.id, role_id=role.id))
    db_session.commit()

    service = PermissionReportService(db_session, session_service=type("Sessions", (), {"revoke_user_sessions": lambda *_args: 0})())
    items = [PermissionReportItem(code="jcc:data:read", display_name="JCC data", description="Read JCC")]
    first = service.report(caller.app_id, items)
    db_session.commit()
    second = service.report(caller.app_id, items)
    db_session.commit()

    permission = db_session.scalar(select(Permission).where(Permission.name == "jcc:data:read"))
    assert permission is not None
    assert permission.owner_app_id == caller.app_id
    assert first.created == 1
    assert first.admin_grants_added == 1
    assert second.created == 0
    assert second.admin_grants_added == 0
    assert db_session.execute(select(role_permissions)).all() == [(role.id, permission.id)]


def test_report_marks_removed_permissions_missing_without_deleting_grant(db_session: DbSession) -> None:
    caller = make_app("app_jcc")
    role = Role(name="admin")
    permission = Permission(name="jcc:data:read", owner_app_id=caller.app_id)
    db_session.add_all([caller, role, permission])
    db_session.flush()
    db_session.execute(role_permissions.insert().values(role_id=role.id, permission_id=permission.id))
    db_session.commit()

    summary = PermissionReportService(db_session).report(caller.app_id, [])
    db_session.commit()

    db_session.refresh(permission)
    assert summary.marked_missing == 1
    assert permission.is_declared is False
    assert permission.missing_at is not None
    assert db_session.execute(select(role_permissions)).all() == [(role.id, permission.id)]


def test_report_revokes_sessions_for_changed_permission_users(db_session: DbSession) -> None:
    caller = make_app("app_jcc")
    role = Role(name="admin")
    user = User(email="admin@example.com", hashed_password="hash")
    permission = Permission(name="jcc:data:read", owner_app_id=caller.app_id)
    db_session.add_all([caller, role, user, permission])
    db_session.flush()
    db_session.execute(user_roles.insert().values(user_id=user.id, role_id=role.id))
    db_session.execute(role_permissions.insert().values(role_id=role.id, permission_id=permission.id))
    db_session.commit()

    class RecordingSessions:
        def __init__(self) -> None:
            self.calls: list[tuple[int, str]] = []

        def revoke_user_sessions(self, user_id: int, reason: str) -> int:
            self.calls.append((user_id, reason))
            return 1

    sessions = RecordingSessions()
    summary = PermissionReportService(db_session, session_service=sessions).report(caller.app_id, [])
    assert summary.sessions_revoked == 1
    assert sessions.calls == [(user.id, "permission_report")]


def test_report_rejects_name_owned_by_main_app_without_writing(db_session: DbSession) -> None:
    caller = make_app("app_jcc")
    role = Role(name="admin")
    db_session.add_all([caller, role, Permission(name="jcc:data:read")])
    db_session.commit()

    with pytest.raises(PermissionReportNameConflictError):
        PermissionReportService(db_session).report(
            caller.app_id,
            [PermissionReportItem(code="jcc:data:read", display_name="JCC data")],
        )
    db_session.rollback()
    assert db_session.scalar(select(Permission).where(Permission.name == "jcc:data:read")).owner_app_id is None
