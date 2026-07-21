import pytest

import app.api.auth as auth_api
from app.schemas.auth import LoginRequest
from app.schemas.auth import LogoutResponse, TokenResponse, UserResponse


def test_login_success_returns_tokens(client, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAuthService:
        def __init__(self, db) -> None:
            self.db = db

        def login(self, payload: LoginRequest) -> TokenResponse:
            assert str(payload.username).lower() == "admin@example.com"
            return TokenResponse(access_token="access", refresh_token="refresh", expires_in=3600)

    monkeypatch.setattr(auth_api, "AuthService", FakeAuthService)

    response = client.post("/auth/login", json={"username": "admin@example.com", "password": "password123"})

    assert response.status_code == 200
    assert response.json()["access_token"] == "access"
    assert response.json()["refresh_token"] == "refresh"
    assert response.headers["X-Request-ID"]


def test_login_failure_returns_401(client, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAuthService:
        def __init__(self, db) -> None:
            self.db = db

        def login(self, payload: LoginRequest) -> TokenResponse:
            raise ValueError("invalid credentials")

    monkeypatch.setattr(auth_api, "AuthService", FakeAuthService)

    response = client.post("/auth/login", json={"username": "admin@example.com", "password": "wrong"})

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid credentials"
    assert response.headers["X-Request-ID"]


def test_refresh_success_returns_new_tokens(client, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAuthService:
        def __init__(self, db) -> None:
            self.db = db

        def refresh(self, refresh_token: str) -> TokenResponse:
            assert refresh_token == "refresh"
            return TokenResponse(access_token="new-access", refresh_token="new-refresh", expires_in=3600)

    monkeypatch.setattr(auth_api, "AuthService", FakeAuthService)

    response = client.post("/auth/refresh", json={"refresh_token": "refresh"})

    assert response.status_code == 200
    assert response.json()["access_token"] == "new-access"
    assert response.json()["refresh_token"] == "new-refresh"


def test_refresh_failure_returns_401(client, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAuthService:
        def __init__(self, db) -> None:
            self.db = db

        def refresh(self, refresh_token: str) -> TokenResponse:
            raise ValueError("invalid refresh token")

    monkeypatch.setattr(auth_api, "AuthService", FakeAuthService)

    response = client.post("/auth/refresh", json={"refresh_token": "bad"})

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid refresh token"


def test_logout_success_returns_message(client, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAuthService:
        def __init__(self, db) -> None:
            self.db = db

        def logout(self, access_token: str) -> LogoutResponse:
            assert access_token == "access"
            return LogoutResponse(message="logged out")

    monkeypatch.setattr(auth_api, "AuthService", FakeAuthService)

    response = client.post("/auth/logout", headers={"Authorization": "Bearer access"})

    assert response.status_code == 200
    assert response.json() == {"message": "logged out"}


def test_logout_missing_token_returns_401(client) -> None:
    response = client.post("/auth/logout")

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid access token"


def test_me_success_returns_current_user(client, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAuthService:
        def __init__(self, db) -> None:
            self.db = db

        def current_user(self, access_token: str) -> UserResponse:
            assert access_token == "access"
            return UserResponse(id="1", username="admin@example.com", roles=["admin"])

    monkeypatch.setattr(auth_api, "AuthService", FakeAuthService)

    response = client.get("/auth/me", headers={"Authorization": "Bearer access"})

    assert response.status_code == 200
    assert response.json()["id"] == "1"
    assert response.json()["roles"] == ["admin"]


def test_me_blacklisted_token_returns_401(client, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAuthService:
        def __init__(self, db) -> None:
            self.db = db

        def current_user(self, access_token: str) -> UserResponse:
            raise ValueError("token is blacklisted")

    monkeypatch.setattr(auth_api, "AuthService", FakeAuthService)

    response = client.get("/auth/me", headers={"Authorization": "Bearer access"})

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid access token"


def test_me_revoked_session_returns_401(client, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAuthService:
        def __init__(self, db) -> None:
            self.db = db

        def current_user(self, access_token: str) -> UserResponse:
            raise ValueError("session is revoked")

    monkeypatch.setattr(auth_api, "AuthService", FakeAuthService)

    response = client.get("/auth/me", headers={"Authorization": "Bearer access"})

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid access token"
