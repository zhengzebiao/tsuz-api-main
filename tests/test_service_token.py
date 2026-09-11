from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.internal import router as internal_router
from app.api.internal_oauth import router as oauth_router
from app.core.config import settings
from app.core.database import Base, get_db
from app.core.logging import RequestIdMiddleware
from app.core.security import hash_app_secret
from app.models.app import App
from app.models.app_service_grant import AppServiceGrant
from app.models.resource_scope import ResourceScope
from app.models.user import User
from tests.conftest import TEST_PRIVATE_KEY, TEST_PUBLIC_KEY

CALLER_SECRET = "app_secret_caller_example_value_123456"


@pytest.fixture
def token_context(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, DbSession, App, App, AppServiceGrant]]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    actor = User(email="admin@example.com", hashed_password="hash")
    caller = App(
        app_id="app_caller",
        app_secret_hash=hash_app_secret(CALLER_SECRET),
        name="Caller",
        access_url="https://caller.example.com",
        service_account_name="Caller Service",
    )
    target = App(
        app_id="app_main",
        app_secret_hash=hash_app_secret("app_secret_target_example_value_123456"),
        name="Main",
        icon_url=None,
        access_url="https://main.example.com",
        service_account_name="Main Service",
    )
    db.add_all([actor, caller, target])
    db.flush()
    scope = ResourceScope(
        target_app_id=target.app_id,
        scope_code="main:application:read",
    )
    db.add(scope)
    db.flush()
    grant = AppServiceGrant(
        caller_app_id=caller.app_id,
        scope_id=scope.id,
        created_by=actor.id,
        valid_from=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1),
    )
    db.add(grant)
    db.commit()

    monkeypatch.setattr(settings, "jwt_private_key", TEST_PRIVATE_KEY)
    monkeypatch.setattr(settings, "service_token_public_key", TEST_PUBLIC_KEY)
    monkeypatch.setattr(settings, "service_token_issuer", "tsuz-api-main-test")
    monkeypatch.setattr(settings, "service_token_expire_seconds", 300)
    monkeypatch.setattr(settings, "service_token_clock_skew_seconds", 5)
    monkeypatch.setattr(settings, "main_app_id", target.app_id)

    def override_db() -> Iterator[DbSession]:
        yield db

    application = FastAPI()
    application.add_middleware(RequestIdMiddleware)
    application.include_router(oauth_router)
    application.include_router(internal_router)
    application.dependency_overrides[get_db] = override_db
    try:
        with TestClient(application) as client:
            yield client, db, caller, target, grant
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def token_request(client: TestClient, *, secret: str = CALLER_SECRET, **overrides):
    data = {
        "grant_type": "client_credentials",
        "audience": "app_main",
        "scope": "main:application:read",
    }
    data.update(overrides)
    return client.post(
        "/internal/oauth/token",
        auth=("app_caller", secret),
        data=data,
    )


def test_service_token_openapi_declares_basic_auth_and_form_fields(token_context) -> None:
    client, _db, _caller, _target, _grant = token_context

    operation = client.app.openapi()["paths"]["/internal/oauth/token"]["post"]
    request_body = operation["requestBody"]
    form_schema = request_body["content"]["application/x-www-form-urlencoded"]["schema"]

    assert operation["security"] == [{"ServiceClientBasic": []}]
    assert request_body["required"] is True
    assert set(form_schema["properties"]) == {"grant_type", "audience", "scope"}
    assert set(form_schema["required"]) == {"grant_type", "audience", "scope"}
    assert form_schema["properties"]["audience"]["maxLength"] == 64
    assert form_schema["properties"]["scope"]["maxLength"] == 4096


def test_service_token_success_has_strict_claims_and_no_store(token_context) -> None:
    client, _db, caller, target, _grant = token_context

    response = token_request(client)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 300
    assert body["scope"] == "main:application:read"
    claims = jwt.decode(
        body["access_token"],
        TEST_PUBLIC_KEY,
        algorithms=["RS256"],
        issuer="tsuz-api-main-test",
        audience=target.app_id,
        options={"strict_aud": True},
    )
    assert claims["sub"] == caller.app_id
    assert claims["aud"] == target.app_id
    assert claims["token_use"] == "service"
    assert claims["scope"] == "main:application:read"
    assert claims["exp"] - claims["iat"] == 300
    assert claims["nbf"] == claims["iat"]
    assert isinstance(claims["jti"], str) and claims["jti"]

    internal = client.get(
        f"/internal/v1/applications/{caller.app_id}",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert internal.status_code == 200
    internal_body = internal.json()
    assert internal_body["app_id"] == caller.app_id
    assert "app_secret" not in str(internal_body)
    assert "app_secret_hash" not in str(internal_body)


def test_service_token_invalid_client_is_indistinguishable_and_no_store(token_context) -> None:
    client, _db, _caller, _target, _grant = token_context

    missing = client.post(
        "/internal/oauth/token",
        data={
            "grant_type": "client_credentials",
            "audience": "app_main",
            "scope": "main:application:read",
        },
    )
    wrong_secret = token_request(client, secret="wrong-secret")
    missing_app = client.post(
        "/internal/oauth/token",
        auth=("missing-app", CALLER_SECRET),
        data={
            "grant_type": "client_credentials",
            "audience": "app_main",
            "scope": "main:application:read",
        },
    )

    for response in (missing, wrong_secret, missing_app):
        assert response.status_code == 401
        assert response.json() == {"error": "invalid_client"}
        assert response.headers["Cache-Control"] == "no-store"


def test_service_token_rejects_invalid_request_target_and_scope(token_context) -> None:
    client, db, caller, target, grant = token_context

    malformed = client.post(
        "/internal/oauth/token",
        auth=(caller.app_id, CALLER_SECRET),
        data={"grant_type": "password", "audience": target.app_id, "scope": "main:application:read"},
    )
    duplicate_scope = token_request(
        client,
        scope="main:application:read main:application:read",
    )
    unknown_scope = token_request(client, scope="main:application:read main:other:read")
    wrong_target = token_request(client, audience="missing")
    extra_field = token_request(client, caller="injected")

    for response in (malformed, duplicate_scope, extra_field):
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_request"
        assert response.headers["Cache-Control"] == "no-store"
    assert unknown_scope.status_code == 400
    assert unknown_scope.json()["error"] == "invalid_scope"
    assert wrong_target.status_code == 400
    assert wrong_target.json()["error"] == "invalid_target"

    grant.status = "revoked"
    db.commit()
    revoked = token_request(client)
    assert revoked.status_code == 400
    assert revoked.json()["error"] == "invalid_scope"


def test_service_token_fails_closed_for_disabled_apps_and_scope(token_context) -> None:
    client, db, caller, target, grant = token_context

    caller.is_enabled = False
    db.commit()
    assert token_request(client).status_code == 401
    caller.is_enabled = True
    target.is_enabled = False
    db.commit()
    assert token_request(client).json()["error"] == "invalid_target"
    target.is_enabled = True
    scope = db.get(ResourceScope, grant.scope_id)
    assert scope is not None
    scope.is_enabled = False
    db.commit()
    assert token_request(client).json()["error"] == "invalid_scope"


def make_service_token(**overrides) -> str:
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "iss": "tsuz-api-main-test",
        "sub": "app_caller",
        "aud": "app_main",
        "token_use": "service",
        "scope": "main:application:read",
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "jti": "jti-service",
    }
    payload.update(overrides)
    return jwt.encode(payload, TEST_PRIVATE_KEY, algorithm="RS256")


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "wrong"},
        {"aud": "wrong"},
        {"aud": ["app_main"]},
        {"token_use": "access"},
        {"sub": ""},
        {"scope": ["main:application:read"]},
        {"iat": "now"},
        {"nbf": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp())},
        {"exp": int((datetime.now(UTC) - timedelta(minutes=5)).timestamp())},
    ],
)
def test_main_service_auth_rejects_invalid_claims(token_context, overrides) -> None:
    client, _db, caller, _target, _grant = token_context
    response = client.get(
        f"/internal/v1/applications/{caller.app_id}",
        headers={"Authorization": f"Bearer {make_service_token(**overrides)}"},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid service token"}


def test_main_service_auth_distinguishes_missing_scope_and_user_token(token_context) -> None:
    client, _db, caller, _target, _grant = token_context
    insufficient = client.get(
        f"/internal/v1/applications/{caller.app_id}",
        headers={"Authorization": f"Bearer {make_service_token(scope='main:other:read')}"},
    )
    user_token = jwt.encode(
        {
            "iss": "tsuz-api-main-test",
            "sub": "user-1",
            "aud": "app_main",
            "scope": "main:application:read",
            "iat": int(datetime.now(UTC).timestamp()),
            "nbf": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
            "jti": "user-jti",
            "sid": "sid",
        },
        TEST_PRIVATE_KEY,
        algorithm="RS256",
    )
    rejected_user = client.get(
        f"/internal/v1/applications/{caller.app_id}",
        headers={"Authorization": f"Bearer {user_token}"},
    )

    assert insufficient.status_code == 403
    assert insufficient.json() == {"detail": "insufficient_scope"}
    assert rejected_user.status_code == 401
