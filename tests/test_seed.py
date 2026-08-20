from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.security import hash_password, verify_password
from app.models.permission import Permission
from app.models.role import Role, role_permissions, user_roles
from app.models.user import User
from app.seed.__main__ import DEFAULT_ROLE, NORMAL_ROLE, get_seed_credentials, seed

TEST_ADMIN_EMAIL = "seed-admin@example.com"
TEST_ADMIN_PASSWORD = "test-seed-password"


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
    seed(db_session, email=TEST_ADMIN_EMAIL, password=TEST_ADMIN_PASSWORD)
    seed(db_session, email=TEST_ADMIN_EMAIL, password=TEST_ADMIN_PASSWORD)
    db_session.commit()

    admin_user = db_session.scalar(select(User).where(User.email == TEST_ADMIN_EMAIL))
    admin_role = db_session.scalar(select(Role).where(Role.name == DEFAULT_ROLE))
    normal_role = db_session.scalar(select(Role).where(Role.name == NORMAL_ROLE))
    assert admin_user is not None
    assert admin_role is not None
    assert normal_role is not None
    assert normal_role.is_enabled is True
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(user_roles)
            .where(
                user_roles.c.user_id == admin_user.id,
                user_roles.c.role_id == admin_role.id,
            )
        )
        == 1
    )
    assert db_session.scalar(select(func.count()).select_from(Permission)) == 0
    assert db_session.scalar(select(func.count()).select_from(role_permissions)) == 0


def test_seed_requires_environment_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEED_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="SEED_ADMIN_EMAIL is required"):
        get_seed_credentials()

    monkeypatch.setenv("SEED_ADMIN_EMAIL", TEST_ADMIN_EMAIL)
    with pytest.raises(RuntimeError, match="SEED_ADMIN_PASSWORD is required"):
        get_seed_credentials()


def test_seed_reads_environment_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEED_ADMIN_EMAIL", f"  {TEST_ADMIN_EMAIL}  ")
    monkeypatch.setenv("SEED_ADMIN_PASSWORD", TEST_ADMIN_PASSWORD)

    assert get_seed_credentials() == (TEST_ADMIN_EMAIL, TEST_ADMIN_PASSWORD)


def test_seed_does_not_reset_existing_password(db_session: DbSession) -> None:
    original_password = "original-password"
    existing_user = User(
        email=TEST_ADMIN_EMAIL,
        hashed_password=hash_password(original_password),
        is_active=True,
    )
    db_session.add(existing_user)
    db_session.commit()

    seed(db_session, email=TEST_ADMIN_EMAIL, password=TEST_ADMIN_PASSWORD)
    db_session.commit()
    db_session.refresh(existing_user)

    assert verify_password(original_password, existing_user.hashed_password)
    assert not verify_password(TEST_ADMIN_PASSWORD, existing_user.hashed_password)


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

    seed(db_session, email=TEST_ADMIN_EMAIL, password=TEST_ADMIN_PASSWORD)
    seed(db_session, email=TEST_ADMIN_EMAIL, password=TEST_ADMIN_PASSWORD)
    db_session.commit()

    preserved = db_session.scalar(select(Permission).where(Permission.name == "legacy:read"))
    assert preserved is not None
    assert preserved.display_name == "Legacy Read"
    assert db_session.scalar(select(func.count()).select_from(Permission)) == 1
    assert db_session.scalar(select(func.count()).select_from(role_permissions)) == 1
