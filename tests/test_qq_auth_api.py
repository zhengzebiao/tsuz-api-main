from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.auth as auth_api
from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.schemas.auth import TokenResponse, UserIdentityResponse
from app.services.authorization_service import AuthenticationError
from app.services.qq_oauth_service import (
    QQOAuthConfigurationError,
    QQOAuthIdentityError,
    QQOAuthProviderError,
    QQOAuthStateError,
    QQOAuthTicketError,
)


@pytest.fixture
def qq_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    testing_session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = testing_session()

    def override_db() -> Iterator[DbSession]:
        yield db

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(settings, "qq_ticket_redirect_uri", "https://client.example.test/login?source=qq")
    try:
        with TestClient(app, follow_redirects=False) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_qq_login_redirects_to_service_authorize_url(
    qq_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeQQOAuthService:
        def __init__(self, db: DbSession) -> None:
            self.db = db

        def build_authorize_url(self) -> str:
            return "https://graph.qq.test/authorize?state=opaque-state"

    monkeypatch.setattr(auth_api, "QQOAuthService", FakeQQOAuthService)

    response = qq_client.get("/auth/qq/login")

    assert response.status_code == 302
    assert response.headers["location"] == "https://graph.qq.test/authorize?state=opaque-state"


@pytest.mark.parametrize("error", [QQOAuthConfigurationError("safe"), QQOAuthStateError("safe")])
def test_qq_login_maps_service_failure_to_503(
    qq_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    class FakeQQOAuthService:
        def __init__(self, db: DbSession) -> None:
            self.db = db

        def build_authorize_url(self) -> str:
            raise error

    monkeypatch.setattr(auth_api, "QQOAuthService", FakeQQOAuthService)

    response = qq_client.get("/auth/qq/login")

    assert response.status_code == 503
    assert response.json() == {"detail": "QQ authentication unavailable"}


def test_qq_callback_redirects_ticket_to_fixed_configured_target(
    qq_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[tuple[str, str]] = []

    class FakeQQOAuthService:
        def __init__(self, db: DbSession) -> None:
            self.db = db

        def complete_authorization(self, code: str, state: str) -> str:
            received.append((code, state))
            return "opaque-ticket"

    monkeypatch.setattr(auth_api, "QQOAuthService", FakeQQOAuthService)

    response = qq_client.get(
        "/auth/qq/callback",
        params={
            "code": "opaque-code",
            "state": "opaque-state",
            "redirect_uri": "https://attacker.example.test/steal",
        },
    )

    assert response.status_code == 302
    assert response.headers["location"] == (
        "https://client.example.test/login?source=qq&ticket=opaque-ticket"
    )
    assert received == [("opaque-code", "opaque-state")]
    assert "attacker.example.test" not in response.headers["location"]


@pytest.mark.parametrize(
    "params,error",
    [
        ({}, None),
        ({"code": "opaque-code"}, None),
        ({"state": "opaque-state"}, None),
        (
            {"code": "provider-secret", "state": "state-secret"},
            QQOAuthProviderError("provider body secret"),
        ),
        (
            {"code": "provider-secret", "state": "state-secret"},
            QQOAuthStateError("state secret"),
        ),
    ],
)
def test_qq_callback_uses_stable_error_redirect_without_sensitive_values(
    qq_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, str],
    error: Exception | None,
) -> None:
    class FakeQQOAuthService:
        def __init__(self, db: DbSession) -> None:
            self.db = db

        def complete_authorization(self, code: str, state: str) -> str:
            assert error is not None
            raise error

    monkeypatch.setattr(auth_api, "QQOAuthService", FakeQQOAuthService)

    response = qq_client.get("/auth/qq/callback", params=params)

    assert response.status_code == 302
    assert response.headers["location"] == (
        "https://client.example.test/login?source=qq&qq_error=oauth_failed"
    )
    assert "provider-secret" not in response.headers["location"]
    assert "state-secret" not in response.headers["location"]
    assert "provider+body" not in response.headers["location"]


def test_qq_callback_missing_redirect_configuration_returns_503(
    qq_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "qq_ticket_redirect_uri", "")

    response = qq_client.get(
        "/auth/qq/callback",
        params={"code": "opaque-code", "state": "opaque-state"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "QQ authentication unavailable"}


def test_qq_exchange_consumes_ticket_then_uses_shared_login_path(
    qq_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    class FakeQQOAuthService:
        def __init__(self, db: DbSession) -> None:
            self.db = db

        def consume_ticket(self, ticket: str) -> int:
            calls.append(("consume", ticket))
            return 42

    class FakeAuthService:
        def __init__(self, db: DbSession) -> None:
            self.db = db

        def complete_login(self, user_id: int) -> TokenResponse:
            calls.append(("login", user_id))
            return TokenResponse(
                access_token="local-access",
                refresh_token="local-refresh",
                expires_in=3600,
            )

    monkeypatch.setattr(auth_api, "QQOAuthService", FakeQQOAuthService)
    monkeypatch.setattr(auth_api, "AuthService", FakeAuthService)

    response = qq_client.post("/auth/qq/exchange", json={"ticket": "opaque-ticket"})

    assert response.status_code == 200
    assert response.json() == {
        "access_token": "local-access",
        "refresh_token": "local-refresh",
        "token_type": "Bearer",
        "expires_in": 3600,
    }
    assert calls == [("consume", "opaque-ticket"), ("login", 42)]


@pytest.mark.parametrize(
    "error",
    [QQOAuthTicketError("ticket secret"), AuthenticationError("invalid user")],
)
def test_qq_exchange_maps_invalid_ticket_or_user_to_401(
    qq_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    class FakeQQOAuthService:
        def __init__(self, db: DbSession) -> None:
            self.db = db

        def consume_ticket(self, ticket: str) -> int:
            if isinstance(error, QQOAuthTicketError):
                raise error
            return 42

    class FakeAuthService:
        def __init__(self, db: DbSession) -> None:
            self.db = db

        def complete_login(self, user_id: int) -> TokenResponse:
            raise error

    monkeypatch.setattr(auth_api, "QQOAuthService", FakeQQOAuthService)
    monkeypatch.setattr(auth_api, "AuthService", FakeAuthService)

    response = qq_client.post("/auth/qq/exchange", json={"ticket": "opaque-ticket"})

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid QQ ticket"}


@pytest.mark.parametrize("error", [QQOAuthConfigurationError("safe"), QQOAuthIdentityError("safe"), RedisError("safe"), SQLAlchemyError("safe")])
def test_qq_exchange_maps_service_failures_to_503(
    qq_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    class FakeQQOAuthService:
        def __init__(self, db: DbSession) -> None:
            self.db = db

        def consume_ticket(self, ticket: str) -> int:
            raise error

    monkeypatch.setattr(auth_api, "QQOAuthService", FakeQQOAuthService)

    response = qq_client.post("/auth/qq/exchange", json={"ticket": "opaque-ticket"})

    assert response.status_code == 503
    assert response.json() == {"detail": "QQ authentication unavailable"}


def test_qq_exchange_maps_login_persistence_failure_to_503(
    qq_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeQQOAuthService:
        def __init__(self, db: DbSession) -> None:
            self.db = db

        def consume_ticket(self, ticket: str) -> int:
            return 42

    class FakeAuthService:
        def __init__(self, db: DbSession) -> None:
            self.db = db

        def complete_login(self, user_id: int) -> TokenResponse:
            raise SQLAlchemyError("safe")

    monkeypatch.setattr(auth_api, "QQOAuthService", FakeQQOAuthService)
    monkeypatch.setattr(auth_api, "AuthService", FakeAuthService)

    response = qq_client.post("/auth/qq/exchange", json={"ticket": "opaque-ticket"})

    assert response.status_code == 503
    assert response.json() == {"detail": "QQ authentication unavailable"}


def test_qq_exchange_request_schema_is_strict_and_bounded(qq_client: TestClient) -> None:
    empty = qq_client.post("/auth/qq/exchange", json={"ticket": ""})
    long = qq_client.post("/auth/qq/exchange", json={"ticket": "x" * 257})
    extra = qq_client.post(
        "/auth/qq/exchange",
        json={"ticket": "opaque", "redirect_uri": "https://attacker.example.test"},
    )

    assert empty.status_code == 422
    assert long.status_code == 422
    assert extra.status_code == 422


def test_safe_identity_schema_excludes_provider_subject_and_internal_id() -> None:
    identity = UserIdentityResponse.model_validate(
        {
            "id": 99,
            "provider": "qq",
            "provider_subject": "openid-secret",
            "display_name": "QQ User",
            "avatar": "https://avatar.example.test/a",
            "verified": True,
        }
    )

    assert identity.model_dump() == {
        "provider": "qq",
        "display_name": "QQ User",
        "avatar": "https://avatar.example.test/a",
        "verified": True,
    }


def test_only_documented_qq_routes_are_registered() -> None:
    paths = app.openapi()["paths"]

    assert "get" in paths["/auth/qq/login"]
    assert "get" in paths["/auth/qq/callback"]
    assert "post" in paths["/auth/qq/exchange"]
    assert "/auth/qq/url" not in paths
