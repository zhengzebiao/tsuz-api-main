from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session as DbSession, sessionmaker

from app.core.database import Base
from app.models.permission import Permission
from app.models.role import Role, role_permissions, user_roles
from app.models.user import User
from app.seed.__main__ import DEFAULT_ADMIN_EMAIL, DEFAULT_ROLE, seed


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


def test_seed_adds_admin_identity_without_creating_permissions(db_session: DbSession) -> None:
    seed(db_session)
    seed(db_session)
    db_session.commit()

    admin_user = db_session.scalar(select(User).where(User.email == DEFAULT_ADMIN_EMAIL))
    admin_role = db_session.scalar(select(Role).where(Role.name == DEFAULT_ROLE))
    assert admin_user is not None
    assert admin_role is not None
    assert db_session.scalar(
        select(func.count())
        .select_from(user_roles)
        .where(
            user_roles.c.user_id == admin_user.id,
            user_roles.c.role_id == admin_role.id,
        )
    ) == 1
    assert db_session.scalar(select(func.count()).select_from(Permission)) == 0
    assert db_session.scalar(select(func.count()).select_from(role_permissions)) == 0


def test_seed_preserves_existing_permissions_and_role_grants(db_session: DbSession) -> None:
    permission = Permission(name="legacy:read", display_name="Legacy Read")
    role = Role(name="legacy-role")
    db_session.add_all((permission, role))
    db_session.flush()
    db_session.execute(
        role_permissions.insert().values(
            role_id=role.id,
            permission_id=permission.id,
        )
    )
    db_session.commit()

    seed(db_session)
    seed(db_session)
    db_session.commit()

    preserved = db_session.scalar(select(Permission).where(Permission.name == "legacy:read"))
    assert preserved is not None
    assert preserved.display_name == "Legacy Read"
    assert db_session.scalar(select(func.count()).select_from(Permission)) == 1
    assert db_session.scalar(select(func.count()).select_from(role_permissions)) == 1
