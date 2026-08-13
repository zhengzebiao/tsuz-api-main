from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session as DbSession, sessionmaker

from app.core.database import Base
from app.models.permission import Permission
from app.models.role import Role, role_permissions
from app.seed.__main__ import DEFAULT_PERMISSIONS, DEFAULT_ROLE, seed


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


def test_seed_adds_all_admin_permissions_idempotently(db_session: DbSession) -> None:
    seed(db_session)
    seed(db_session)
    db_session.commit()

    admin_role = db_session.scalar(select(Role).where(Role.name == DEFAULT_ROLE))
    assert admin_role is not None
    permission_names = set(
        db_session.scalars(
            select(Permission.name)
            .join(role_permissions, role_permissions.c.permission_id == Permission.id)
            .where(role_permissions.c.role_id == admin_role.id)
        ).all()
    )
    assert permission_names == set(DEFAULT_PERMISSIONS)
    assert {
        "app:read",
        "app:create",
        "app:update",
        "app:enable",
        "app:disable",
        "app:regenerate_secret",
        "role:read",
        "role:create",
        "role:update",
        "role:disable",
        "role:enable",
        "user:assign_roles",
    } <= permission_names
    assert "user:write" in permission_names
    assert db_session.scalar(select(func.count()).select_from(Permission)) == len(DEFAULT_PERMISSIONS)
    assert db_session.scalar(select(func.count()).select_from(role_permissions)) == len(DEFAULT_PERMISSIONS)
