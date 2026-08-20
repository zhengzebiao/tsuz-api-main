from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

import app.services.admin_app_service as service_module
from app.core.database import Base
from app.core.security import hash_app_secret, verify_app_secret
from app.models.app import App
from app.models.audit_event import AuditEvent
from app.models.user import User
from app.schemas.admin_app import AdminAppCreate, AdminAppUpdate
from app.services.admin_app_service import (
    AdminAppService,
    AppCreationError,
    AppNotFoundError,
    AppSecretGenerationError,
    AppVersionConflictError,
)


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


def add_actor(db: DbSession) -> User:
    actor = User(
        email="admin@example.com",
        hashed_password="not-used-by-app-service",
        is_active=True,
        is_blacklisted=False,
    )
    db.add(actor)
    db.flush()
    return actor


def add_app(
    db: DbSession,
    *,
    app_id: str,
    name: str,
    is_enabled: bool = True,
    created_at: datetime | None = None,
) -> App:
    app = App(
        app_id=app_id,
        app_secret_hash=hash_app_secret(f"secret-for-{app_id}"),
        name=name,
        icon_url="https://static.example.com/icon.png",
        access_url="https://app.example.com",
        service_account_name=f"{name} Service",
        is_enabled=is_enabled,
    )
    if created_at is not None:
        app.created_at = created_at
        app.updated_at = created_at
        app.secret_updated_at = created_at
    db.add(app)
    db.flush()
    return app


def app_payload() -> AdminAppCreate:
    return AdminAppCreate(
        name="Project Management",
        icon_url="https://static.example.com/project.png",
        access_url="https://project.example.com",
        service_account_name="Project Management Service",
    )


def app_audits(db: DbSession) -> list[AuditEvent]:
    return list(db.scalars(select(AuditEvent).where(AuditEvent.target_type == "app").order_by(AuditEvent.id)))


def test_list_and_get_apps_support_filters_pagination_and_stable_order(db_session: DbSession) -> None:
    created_at = datetime(2026, 8, 12, 10, 30, tzinfo=UTC).replace(tzinfo=None)
    first = add_app(
        db_session,
        app_id="app_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        name="Project Alpha",
        created_at=created_at,
    )
    second = add_app(
        db_session,
        app_id="app_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        name="Project Beta",
        created_at=created_at,
    )
    disabled = add_app(
        db_session,
        app_id="app_cccccccccccccccccccccccccccccccc",
        name="Archive",
        is_enabled=False,
        created_at=datetime(2026, 8, 11, 10, 30, tzinfo=UTC).replace(tzinfo=None),
    )
    db_session.commit()
    service = AdminAppService(db_session)

    apps, total = service.list_apps(page=1, page_size=1, keyword="PROJECT", is_enabled=True)

    assert total == 2
    assert [app.id for app in apps] == [second.id]
    assert service.list_apps(page=2, page_size=1, keyword="project", is_enabled=True)[0][0].id == first.id
    assert service.list_apps(keyword=disabled.app_id.upper())[0][0].id == disabled.id
    assert service.get_app(first.id).app_id == first.app_id
    with pytest.raises(AppNotFoundError, match="APP_NOT_FOUND"):
        service.get_app(999)


def test_create_app_persists_only_hash_and_adds_safe_audit(db_session: DbSession) -> None:
    actor = add_actor(db_session)
    db_session.commit()
    service = AdminAppService(db_session)

    app, app_secret = service.create_app(
        app_payload(),
        actor_user_id=actor.id,
        request_id="req-app-create",
    )

    assert app.id is not None
    assert app.app_id.startswith("app_")
    assert app.app_secret_hash != app_secret
    assert verify_app_secret(app_secret, app.app_secret_hash)
    assert app.is_enabled is True
    audit = app_audits(db_session)[0]
    assert audit.action == "app.created"
    assert audit.target_id == app.id
    assert audit.request_id == "req-app-create"
    assert audit.changes_json is not None
    assert audit.changes_json["app_id"]["to"] == app.app_id
    audit_text = str(audit.changes_json)
    assert app_secret not in audit_text
    assert app.app_secret_hash not in audit_text
    assert "app_secret" not in audit_text


def test_create_app_retries_app_id_collision_in_savepoint(
    db_session: DbSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = add_actor(db_session)
    collision = "app_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    replacement = "app_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    add_app(db_session, app_id=collision, name="Existing")
    db_session.commit()
    generated_ids = iter((collision, replacement))
    monkeypatch.setattr(service_module, "generate_app_id", lambda: next(generated_ids))

    app, _app_secret = AdminAppService(db_session).create_app(
        app_payload(),
        actor_user_id=actor.id,
    )

    assert app.app_id == replacement
    assert db_session.scalar(select(func.count()).select_from(App)) == 2
    assert len(app_audits(db_session)) == 1


def test_create_app_exhausted_collisions_raise_safe_error(
    db_session: DbSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = add_actor(db_session)
    collision = "app_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    add_app(db_session, app_id=collision, name="Existing")
    db_session.commit()
    monkeypatch.setattr(service_module, "generate_app_id", lambda: collision)

    with pytest.raises(AppCreationError, match="APP_CREATION_FAILED") as exc_info:
        AdminAppService(db_session).create_app(app_payload(), actor_user_id=actor.id)

    assert str(exc_info.value) == "APP_CREATION_FAILED"
    assert len(app_audits(db_session)) == 0


def test_update_app_uses_version_and_audits_only_real_changes(db_session: DbSession) -> None:
    actor = add_actor(db_session)
    app = add_app(
        db_session,
        app_id="app_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        name="Old Name",
    )
    db_session.commit()
    service = AdminAppService(db_session)

    updated, changed = service.update_app(
        app.id,
        AdminAppUpdate(name="New Name", icon_url=None, version=app.version),
        actor_user_id=actor.id,
        request_id="req-app-update",
    )

    assert changed is True
    assert updated.name == "New Name"
    assert updated.icon_url is None
    assert updated.version == 2
    audit = app_audits(db_session)[0]
    assert audit.action == "app.updated"
    assert audit.request_id == "req-app-update"
    assert audit.changes_json == {
        "name": {"from": "Old Name", "to": "New Name"},
        "icon_url": {"from": "https://static.example.com/icon.png", "to": None},
    }

    unchanged, changed = service.update_app(
        app.id,
        AdminAppUpdate(name="New Name", version=updated.version),
        actor_user_id=actor.id,
    )
    assert changed is False
    assert unchanged.version == 2
    assert len(app_audits(db_session)) == 1

    with pytest.raises(AppVersionConflictError, match="APP_VERSION_CONFLICT"):
        service.update_app(
            app.id,
            AdminAppUpdate(name="Stale Name", version=1),
            actor_user_id=actor.id,
        )
    assert len(app_audits(db_session)) == 1


def test_disable_and_enable_are_idempotent_and_audited(db_session: DbSession) -> None:
    actor = add_actor(db_session)
    app = add_app(
        db_session,
        app_id="app_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        name="Project",
    )
    db_session.commit()
    service = AdminAppService(db_session)

    disabled, changed = service.disable_app(
        app.id,
        actor_user_id=actor.id,
        reason="maintenance",
        request_id="req-disable",
    )

    assert changed is True
    assert disabled.is_enabled is False
    assert disabled.disabled_at is not None
    assert disabled.disabled_reason == "maintenance"
    assert disabled.version == 2
    first_disabled_at = disabled.disabled_at

    disabled, changed = service.disable_app(
        app.id,
        actor_user_id=actor.id,
        reason="must not replace original reason",
    )
    assert changed is False
    assert disabled.disabled_at == first_disabled_at
    assert disabled.disabled_reason == "maintenance"
    assert disabled.version == 2
    assert len(app_audits(db_session)) == 1

    enabled, changed = service.enable_app(app.id, actor_user_id=actor.id, request_id="req-enable")
    assert changed is True
    assert enabled.is_enabled is True
    assert enabled.disabled_at is None
    assert enabled.disabled_reason is None
    assert enabled.version == 3

    enabled, changed = service.enable_app(app.id, actor_user_id=actor.id)
    assert changed is False
    assert enabled.version == 3
    audits = app_audits(db_session)
    assert [audit.action for audit in audits] == ["app.disabled", "app.enabled"]
    assert audits[0].reason == "maintenance"
    assert audits[0].changes_json["is_enabled"] == {"from": True, "to": False}
    assert audits[1].changes_json["is_enabled"] == {"from": False, "to": True}


def test_status_and_secret_operations_issue_for_update(
    db_session: DbSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = add_actor(db_session)
    app = add_app(
        db_session,
        app_id="app_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        name="Project",
    )
    db_session.commit()
    original_scalar = db_session.scalar
    statements: list[Any] = []

    def scalar_spy(statement: Any, *args: Any, **kwargs: Any) -> Any:
        statements.append(statement)
        return original_scalar(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "scalar", scalar_spy)
    service = AdminAppService(db_session)

    service.disable_app(app.id, actor_user_id=actor.id)
    service.enable_app(app.id, actor_user_id=actor.id)
    service.regenerate_secret(app.id, actor_user_id=actor.id, reason="rotation")

    lock_statements = [
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in statements
        if getattr(statement, "_for_update_arg", None) is not None
    ]
    assert len(lock_statements) == 3
    assert all("FOR UPDATE" in statement for statement in lock_statements)


def test_regenerate_secret_invalidates_old_secret_and_never_audits_credentials(db_session: DbSession) -> None:
    actor = add_actor(db_session)
    app = add_app(
        db_session,
        app_id="app_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        name="Project",
    )
    old_secret = f"secret-for-{app.app_id}"
    old_hash = app.app_secret_hash
    old_secret_updated_at = app.secret_updated_at
    db_session.commit()
    service = AdminAppService(db_session)

    updated, new_secret = service.regenerate_secret(
        app.id,
        actor_user_id=actor.id,
        reason="possible leak",
        request_id="req-secret",
    )

    assert new_secret != old_secret
    assert updated.app_secret_hash != old_hash
    assert verify_app_secret(old_secret, updated.app_secret_hash) is False
    assert verify_app_secret(new_secret, updated.app_secret_hash) is True
    assert updated.secret_updated_at >= old_secret_updated_at
    assert updated.version == 2
    audit = app_audits(db_session)[0]
    assert audit.action == "app.secret_regenerated"
    assert audit.reason == "possible leak"
    assert audit.request_id == "req-secret"
    assert audit.changes_json == {"secret_changed": True}
    audit_text = str(audit.changes_json)
    assert old_secret not in audit_text
    assert new_secret not in audit_text
    assert old_hash not in audit_text
    assert updated.app_secret_hash not in audit_text


def test_regenerate_secret_retries_accidental_same_secret(
    db_session: DbSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = add_actor(db_session)
    app = add_app(
        db_session,
        app_id="app_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        name="Project",
    )
    old_secret = f"secret-for-{app.app_id}"
    replacement = "app_secret_replacement"
    generated_secrets = iter((old_secret, replacement))
    db_session.commit()
    monkeypatch.setattr(service_module, "generate_app_secret", lambda: next(generated_secrets))

    updated, new_secret = AdminAppService(db_session).regenerate_secret(
        app.id,
        actor_user_id=actor.id,
        reason="rotation",
    )

    assert new_secret == replacement
    assert verify_app_secret(replacement, updated.app_secret_hash)
    assert len(app_audits(db_session)) == 1


def test_regenerate_secret_exhaustion_raises_safe_error(
    db_session: DbSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = add_actor(db_session)
    app = add_app(
        db_session,
        app_id="app_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        name="Project",
    )
    old_secret = f"secret-for-{app.app_id}"
    old_hash = app.app_secret_hash
    db_session.commit()
    monkeypatch.setattr(service_module, "generate_app_secret", lambda: old_secret)

    with pytest.raises(AppSecretGenerationError, match="APP_SECRET_GENERATION_FAILED") as exc_info:
        AdminAppService(db_session).regenerate_secret(
            app.id,
            actor_user_id=actor.id,
            reason="rotation",
        )

    assert str(exc_info.value) == "APP_SECRET_GENERATION_FAILED"
    db_session.refresh(app)
    assert app.app_secret_hash == old_hash
    assert len(app_audits(db_session)) == 0


def test_service_leaves_business_change_and_audit_in_caller_transaction(db_session: DbSession) -> None:
    actor = add_actor(db_session)
    app = add_app(
        db_session,
        app_id="app_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        name="Project",
    )
    app_id = app.id
    db_session.commit()

    changed_app, changed = AdminAppService(db_session).disable_app(
        app_id,
        actor_user_id=actor.id,
        reason="rollback test",
    )
    assert changed is True
    assert changed_app.is_enabled is False
    assert len(app_audits(db_session)) == 1

    db_session.rollback()

    restored = db_session.get(App, app_id)
    assert restored is not None
    assert restored.is_enabled is True
    assert restored.disabled_at is None
    assert restored.disabled_reason is None
    assert restored.version == 1
    assert app_audits(db_session) == []


def test_locked_operations_raise_safe_not_found_error(db_session: DbSession) -> None:
    actor = add_actor(db_session)
    db_session.commit()
    service = AdminAppService(db_session)

    with pytest.raises(AppNotFoundError, match="APP_NOT_FOUND") as exc_info:
        service.regenerate_secret(999, actor_user_id=actor.id, reason="rotation")

    assert str(exc_info.value) == "APP_NOT_FOUND"
