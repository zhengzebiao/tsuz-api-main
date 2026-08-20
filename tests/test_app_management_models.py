from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.app import App


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


def make_app(*, app_id: str = "app_2d92f64361ea4e249f5c9a0de38bc092") -> App:
    return App(
        app_id=app_id,
        app_secret_hash="a" * 64,
        name="Project Management",
        icon_url="https://static.example.com/project.png",
        access_url="https://project.example.com",
        service_account_name="Project Management Service",
    )


def test_app_management_fields_have_expected_defaults(db_session: DbSession) -> None:
    app = make_app()
    db_session.add(app)
    db_session.commit()
    db_session.refresh(app)

    assert app.is_enabled is True
    assert app.disabled_at is None
    assert app.disabled_reason is None
    assert isinstance(app.secret_updated_at, datetime)
    assert isinstance(app.created_at, datetime)
    assert isinstance(app.updated_at, datetime)
    assert app.version == 1


def test_app_management_fields_can_be_persisted(db_session: DbSession) -> None:
    disabled_at = datetime(2026, 8, 12, 10, 30, tzinfo=UTC).replace(tzinfo=None)
    app = make_app()
    app.is_enabled = False
    app.disabled_at = disabled_at
    app.disabled_reason = "maintenance"
    db_session.add(app)
    db_session.commit()
    db_session.refresh(app)

    assert app.app_secret_hash == "a" * 64
    assert app.name == "Project Management"
    assert app.icon_url == "https://static.example.com/project.png"
    assert app.access_url == "https://project.example.com"
    assert app.service_account_name == "Project Management Service"
    assert app.is_enabled is False
    assert app.disabled_at == disabled_at
    assert app.disabled_reason == "maintenance"


def test_app_id_must_be_unique(db_session: DbSession) -> None:
    db_session.add(make_app())
    db_session.commit()
    db_session.add(make_app())

    with pytest.raises(IntegrityError):
        db_session.commit()
