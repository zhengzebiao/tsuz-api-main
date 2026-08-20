from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.permission import Permission
from app.models.permission_endpoint import PermissionEndpoint
from app.models.role import Role, role_permissions


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


def test_permission_management_fields_have_expected_defaults(db_session: DbSession) -> None:
    permission = Permission(name="permission:read")
    db_session.add(permission)
    db_session.commit()
    db_session.refresh(permission)

    assert permission.id > 0
    assert permission.name == "permission:read"
    assert permission.display_name == ""
    assert permission.description == ""
    assert permission.is_declared is True
    assert permission.is_enabled is True
    assert permission.disabled_at is None
    assert permission.disabled_reason is None
    assert permission.missing_at is None
    assert isinstance(permission.created_at, datetime)
    assert isinstance(permission.updated_at, datetime)
    assert permission.version == 1


def test_permission_ids_remain_auto_incrementing_integers(db_session: DbSession) -> None:
    first = Permission(name="permission:first")
    second = Permission(name="permission:second")
    db_session.add_all((first, second))
    db_session.commit()

    assert isinstance(first.id, int)
    assert isinstance(second.id, int)
    assert second.id == first.id + 1


def test_permission_management_fields_can_be_persisted(db_session: DbSession) -> None:
    disabled_at = datetime(2026, 8, 13, 10, 30, tzinfo=UTC).replace(tzinfo=None)
    missing_at = datetime(2026, 8, 13, 11, 30, tzinfo=UTC).replace(tzinfo=None)
    permission = Permission(
        name="permission:update",
        display_name="Update permissions",
        description="Edit permission display information",
        is_declared=False,
        is_enabled=False,
        disabled_at=disabled_at,
        disabled_reason="emergency suspension",
        missing_at=missing_at,
        version=3,
    )
    db_session.add(permission)
    db_session.commit()
    db_session.refresh(permission)

    assert permission.display_name == "Update permissions"
    assert permission.description == "Edit permission display information"
    assert permission.is_declared is False
    assert permission.is_enabled is False
    assert permission.disabled_at == disabled_at
    assert permission.disabled_reason == "emergency suspension"
    assert permission.missing_at == missing_at
    assert permission.version == 3


def test_permission_name_must_remain_unique(db_session: DbSession) -> None:
    db_session.add(Permission(name="permission:disable"))
    db_session.commit()
    db_session.add(Permission(name="permission:disable"))

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_permission_model_metadata_matches_management_schema() -> None:
    columns = Permission.__table__.c
    indexes = {index.name: index for index in Permission.__table__.indexes}

    assert columns.id.type.python_type is int
    assert columns.name.type.length == 128
    assert columns.display_name.type.length == 128
    assert columns.description.type.length == 255
    assert columns.disabled_reason.type.length == 500
    assert columns.id.primary_key is True
    assert columns.name.nullable is False
    assert columns.display_name.nullable is False
    assert columns.description.nullable is False
    assert columns.is_declared.nullable is False
    assert columns.is_enabled.nullable is False
    assert columns.disabled_at.nullable is True
    assert columns.disabled_reason.nullable is True
    assert columns.missing_at.nullable is True
    assert columns.created_at.nullable is False
    assert columns.updated_at.nullable is False
    assert columns.version.nullable is False
    assert indexes["ix_permissions_name"].unique is True
    assert indexes["ix_permissions_is_declared"].unique is False
    assert indexes["ix_permissions_is_enabled"].unique is False


def test_permission_endpoint_metadata_matches_binding_schema() -> None:
    table = PermissionEndpoint.__table__
    columns = table.c
    indexes = {index.name: index for index in table.indexes}
    primary_key_columns = [column.name for column in table.primary_key.columns]
    foreign_key = next(iter(columns.permission_id.foreign_keys))

    assert primary_key_columns == ["permission_id", "http_method", "path"]
    assert columns.permission_id.type.python_type is int
    assert columns.http_method.type.length == 16
    assert columns.path.type.length == 2048
    assert columns.route_name.type.length == 255
    assert all(column.nullable is False for column in columns)
    assert foreign_key.target_fullname == "permissions.id"
    assert foreign_key.ondelete == "CASCADE"
    assert indexes["ix_permission_endpoints_http_method_path"].unique is False
    assert [column.name for column in indexes["ix_permission_endpoints_http_method_path"].columns] == [
        "http_method",
        "path",
    ]


def test_permission_endpoint_composite_key_rejects_duplicate_binding(db_session: DbSession) -> None:
    permission = Permission(name="app:read")
    db_session.add(permission)
    db_session.flush()
    binding = {
        "permission_id": permission.id,
        "http_method": "GET",
        "path": "/admin/apps",
        "route_name": "list_apps",
    }
    db_session.execute(PermissionEndpoint.__table__.insert().values(**binding))
    db_session.commit()

    with pytest.raises(IntegrityError):
        db_session.execute(PermissionEndpoint.__table__.insert().values(**binding))
        db_session.commit()


def test_permission_endpoint_allows_different_methods_and_permissions(db_session: DbSession) -> None:
    read_permission = Permission(name="app:read")
    update_permission = Permission(name="app:update")
    db_session.add_all((read_permission, update_permission))
    db_session.flush()
    db_session.add_all(
        (
            PermissionEndpoint(
                permission_id=read_permission.id,
                http_method="GET",
                path="/admin/apps/{app_id}",
                route_name="get_app",
            ),
            PermissionEndpoint(
                permission_id=update_permission.id,
                http_method="GET",
                path="/admin/apps/{app_id}",
                route_name="get_app",
            ),
            PermissionEndpoint(
                permission_id=update_permission.id,
                http_method="PATCH",
                path="/admin/apps/{app_id}",
                route_name="update_app",
            ),
        )
    )
    db_session.commit()

    assert db_session.query(PermissionEndpoint).count() == 3


def test_existing_role_permission_association_remains_available(db_session: DbSession) -> None:
    role = Role(name="permission-auditor")
    permission = Permission(name="permission:audit", display_name="Audit permissions")
    db_session.add_all((role, permission))
    db_session.flush()
    db_session.execute(
        role_permissions.insert().values(
            role_id=role.id,
            permission_id=permission.id,
        )
    )
    db_session.commit()

    assert db_session.execute(role_permissions.select()).one() == (role.id, permission.id)
