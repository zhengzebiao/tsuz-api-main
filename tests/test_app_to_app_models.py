from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.app import App
from app.models.app_service_grant import AppServiceGrant
from app.models.resource_scope import ResourceScope
from app.models.user import User
from app.schemas.service_authorization import AppServiceGrantCreate, ResourceScopeCreate
from app.services.app_service_grant_service import (
    AppServiceGrantRevokedError,
    AppServiceGrantService,
)
from app.services.resource_scope_service import (
    ResourceScopeAlreadyExistsError,
    ResourceScopeService,
)


@pytest.fixture
def db_session() -> Iterator[DbSession]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
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


def seed_context(db: DbSession) -> tuple[User, App, App]:
    actor = User(email="actor@example.com", hashed_password="hash")
    caller = make_app("app_caller")
    target = make_app("app_target")
    db.add_all([actor, caller, target])
    db.commit()
    return actor, caller, target


def test_resource_scope_constraints_and_lifecycle(db_session: DbSession) -> None:
    actor, _caller, target = seed_context(db_session)
    service = ResourceScopeService(db_session)
    scope = service.create_scope(
        ResourceScopeCreate(
            target_app_id=target.app_id,
            scope_code="target:record:read",
            description="Read records",
        ),
        actor_user_id=actor.id,
        request_id="scope-create",
    )
    db_session.commit()

    assert scope.is_enabled is True
    assert service.disable_scope(scope.id, actor_user_id=actor.id)[1] is True
    assert service.disable_scope(scope.id, actor_user_id=actor.id)[1] is False
    assert service.enable_scope(scope.id, actor_user_id=actor.id)[1] is True
    db_session.commit()

    with pytest.raises(ResourceScopeAlreadyExistsError):
        service.create_scope(
            ResourceScopeCreate(
                target_app_id=target.app_id,
                scope_code="target:record:read",
            ),
            actor_user_id=actor.id,
        )


def test_grant_effective_window_idempotency_and_revocation(db_session: DbSession) -> None:
    actor, caller, target = seed_context(db_session)
    scope = ResourceScopeService(db_session).create_scope(
        ResourceScopeCreate(
            target_app_id=target.app_id,
            scope_code="target:record:read",
        ),
        actor_user_id=actor.id,
    )
    db_session.commit()
    now = datetime.now(UTC).replace(tzinfo=None)
    payload = AppServiceGrantCreate(
        caller_app_id=caller.app_id,
        scope_id=scope.id,
        valid_from=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=5),
    )
    service = AppServiceGrantService(db_session)
    record, changed = service.create_grant(payload, actor_user_id=actor.id)
    db_session.commit()

    assert changed is True
    repeated, repeated_changed = service.create_grant(payload, actor_user_id=actor.id)
    assert repeated.grant.id == record.grant.id
    assert repeated_changed is False
    assert service.effective_scopes(
        caller_app_id=caller.app_id,
        target_app_id=target.app_id,
        now=now,
    ) == {"target:record:read"}

    revoked, revoked_changed = service.revoke_grant(
        record.grant.id,
        actor_user_id=actor.id,
        reason="no longer needed",
    )
    db_session.commit()
    assert revoked_changed is True
    assert revoked.grant.status == "revoked"
    assert service.effective_scopes(
        caller_app_id=caller.app_id,
        target_app_id=target.app_id,
        now=now,
    ) == set()
    with pytest.raises(AppServiceGrantRevokedError):
        service.create_grant(payload, actor_user_id=actor.id)


def test_database_rejects_duplicate_scope_and_grant(db_session: DbSession) -> None:
    actor, caller, target = seed_context(db_session)
    scope = ResourceScope(
        target_app_id=target.app_id,
        scope_code="target:record:read",
    )
    db_session.add(scope)
    db_session.commit()
    db_session.add(
        ResourceScope(
            target_app_id=target.app_id,
            scope_code="target:record:read",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    db_session.add(
        AppServiceGrant(
            caller_app_id=caller.app_id,
            scope_id=scope.id,
            created_by=actor.id,
        )
    )
    db_session.commit()
    db_session.add(
        AppServiceGrant(
            caller_app_id=caller.app_id,
            scope_id=scope.id,
            created_by=actor.id,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    assert db_session.scalar(select(ResourceScope.scope_code)) == "target:record:read"
