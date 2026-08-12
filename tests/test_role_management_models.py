from collections.abc import Iterator
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession, sessionmaker

from app.core.database import Base
from app.models.permission import Permission
from app.models.role import Role, role_permissions, user_roles
from app.models.user import User


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


def test_role_management_fields_have_expected_defaults(db_session: DbSession) -> None:
    role = Role(name="auditor")
    db_session.add(role)
    db_session.commit()
    db_session.refresh(role)

    assert role.description == ""
    assert role.is_enabled is True
    assert role.disabled_at is None
    assert role.disabled_reason is None
    assert isinstance(role.created_at, datetime)
    assert isinstance(role.updated_at, datetime)
    assert role.version == 1


def test_role_management_fields_can_be_persisted(db_session: DbSession) -> None:
    disabled_at = datetime(2026, 8, 12, 12, 30)
    role = Role(
        name="operator",
        description="Operations role",
        is_enabled=False,
        disabled_at=disabled_at,
        disabled_reason="temporary suspension",
        version=2,
    )
    db_session.add(role)
    db_session.commit()
    db_session.refresh(role)

    assert role.description == "Operations role"
    assert role.is_enabled is False
    assert role.disabled_at == disabled_at
    assert role.disabled_reason == "temporary suspension"
    assert role.version == 2


def test_role_name_must_remain_unique(db_session: DbSession) -> None:
    db_session.add(Role(name="reviewer"))
    db_session.commit()
    db_session.add(Role(name="reviewer"))

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_role_model_metadata_matches_management_schema() -> None:
    columns = Role.__table__.c
    indexes = {index.name: index for index in Role.__table__.indexes}

    assert columns.name.type.length == 64
    assert columns.description.type.length == 255
    assert columns.disabled_reason.type.length == 500
    assert columns.description.nullable is False
    assert columns.is_enabled.nullable is False
    assert columns.disabled_at.nullable is True
    assert columns.disabled_reason.nullable is True
    assert columns.created_at.nullable is False
    assert columns.updated_at.nullable is False
    assert columns.version.nullable is False
    assert indexes["ix_roles_name"].unique is True
    assert indexes["ix_roles_is_enabled"].unique is False


def test_existing_role_associations_remain_available(db_session: DbSession) -> None:
    user = User(email="role-user@example.com", hashed_password="hashed-password")
    role = Role(name="associated-role")
    permission = Permission(name="role:test", description="Test role permission")
    db_session.add_all((user, role, permission))
    db_session.flush()
    db_session.execute(user_roles.insert().values(user_id=user.id, role_id=role.id))
    db_session.execute(role_permissions.insert().values(role_id=role.id, permission_id=permission.id))
    db_session.commit()

    assert db_session.execute(user_roles.select()).one() == (user.id, role.id)
    assert db_session.execute(role_permissions.select()).one() == (role.id, permission.id)
