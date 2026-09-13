from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as DbSession, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.internal_permissions import router
from app.core.config import settings
from app.core.database import Base, get_db
from app.core.logging import RequestIdMiddleware
from app.models.app import App
from app.models.permission import Permission
from app.models.role import Role, role_permissions
from app.models.user import User
from tests.conftest import TEST_PRIVATE_KEY, TEST_PUBLIC_KEY


def token(scope: str = "main:permission:report") -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": "report-test",
            "sub": "app_jcc",
            "aud": "main-test",
            "token_use": "service",
            "scope": scope,
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "jti": "report-jti",
        },
        TEST_PRIVATE_KEY,
        algorithm="RS256",
    )


def test_internal_permission_report_creates_and_repeats() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    caller = App(app_id="app_jcc", app_secret_hash="a" * 64, name="JCC", access_url="https://jcc.example", service_account_name="jcc")
    db.add_all([caller, Role(name="admin"), User(email="admin@example.com", hashed_password="hash")])
    db.commit()
    settings.service_token_public_key = TEST_PUBLIC_KEY
    settings.service_token_issuer = "report-test"
    settings.main_app_id = "main-test"

    def override_db() -> Iterator[DbSession]:
        yield db

    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    app.include_router(router)
    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {token()}", "X-Request-ID": "report-1"}
            payload = {"permissions": [{"code": "jcc:data:read", "display_name": "JCC data"}]}
            first = client.put("/internal/v1/permissions/report", headers=headers, json=payload)
            second = client.put("/internal/v1/permissions/report", headers=headers, json=payload)
        assert first.status_code == 200
        assert first.json()["created"] == 1
        assert second.json()["created"] == 0
        permission = db.scalar(select(Permission).where(Permission.name == "jcc:data:read"))
        role = db.scalar(select(Role).where(Role.name == "admin"))
        assert permission is not None and role is not None
        assert db.execute(select(role_permissions).where(role_permissions.c.permission_id == permission.id)).all() == [(role.id, permission.id)]
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_internal_permission_report_rejects_missing_scope() -> None:
    assert token(scope="jcc:stats:read")


@pytest.mark.parametrize(
    "payload",
    [
        {"permissions": [{"code": "jcc:data:read", "display_name": "JCC", "extra": 1}]},
        {"permissions": [{"code": "jcc:data:read", "display_name": "JCC"}, {"code": "jcc:data:read", "display_name": "JCC"}]},
    ],
)
def test_permission_report_schema_rejects_invalid_payloads(payload: dict) -> None:
    from pydantic import ValidationError
    from app.schemas.internal_permission_report import PermissionReportRequest

    with pytest.raises(ValidationError):
        PermissionReportRequest.model_validate(payload)
