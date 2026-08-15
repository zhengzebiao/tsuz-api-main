import pytest

import app.api.auth as auth_api
from app.schemas.auth import (
    EmailChallengeResponse,
    PasswordForgotCodeResponse,
    PasswordResetResponse,
    TokenResponse,
)
from app.services.email_auth_service import (
    EmailAlreadyRegisteredError,
    EmailAuthConfigurationError,
)
from app.services.tencent_ses_service import EmailProviderError
from app.services.verification_challenge_service import (
    ChallengeCodeError,
    ChallengeRateLimitError,
)


class FakeEmailAuthService:
    error: Exception | None = None
    calls: list[tuple] = []

    def __init__(self, db) -> None:
        self.db = db

    @classmethod
    def reset(cls) -> None:
        cls.error = None
        cls.calls = []

    def _raise(self) -> None:
        if self.error is not None:
            raise self.error

    def send_registration_code(self, email: str, client_ip: str) -> EmailChallengeResponse:
        self.calls.append(("register_code", email, client_ip))
        self._raise()
        return EmailChallengeResponse(
            challenge_id="challenge-register",
            expires_in=600,
            resend_after=60,
        )

    def register(self, email: str, challenge_id: str, code: str, password: str) -> TokenResponse:
        self.calls.append(("register", email, challenge_id, code, password))
        self._raise()
        return TokenResponse(access_token="access", refresh_token="refresh", expires_in=3600)

    def login(self, email: str, password: str) -> TokenResponse:
        self.calls.append(("login", email, password))
        self._raise()
        return TokenResponse(access_token="access", refresh_token="refresh", expires_in=3600)

    def send_password_reset_code(self, email: str, client_ip: str) -> PasswordForgotCodeResponse:
        self.calls.append(("forgot", email, client_ip))
        self._raise()
        return PasswordForgotCodeResponse(
            message="如果邮箱已注册，验证码将发送到该邮箱",
            challenge_id="challenge-reset",
            expires_in=600,
            resend_after=60,
        )

    def reset_password(
        self,
        email: str,
        challenge_id: str,
        code: str,
        password: str,
    ) -> PasswordResetResponse:
        self.calls.append(("reset", email, challenge_id, code, password))
        self._raise()
        return PasswordResetResponse(message="密码重置成功，请使用新密码登录")


@pytest.fixture(autouse=True)
def fake_email_service(monkeypatch: pytest.MonkeyPatch):
    FakeEmailAuthService.reset()
    monkeypatch.setattr(auth_api, "EmailAuthService", FakeEmailAuthService)
    monkeypatch.setattr(auth_api, "get_client_ip", lambda request: "192.0.2.10")
    yield FakeEmailAuthService


def test_email_auth_routes_are_registered(client) -> None:
    paths = client.app.openapi()["paths"]

    for path in (
        "/auth/email/register/code",
        "/auth/email/register",
        "/auth/email/login",
        "/auth/password/forgot/code",
        "/auth/password/reset",
    ):
        assert "post" in paths[path]


def test_registration_code_returns_public_challenge_shape(client) -> None:
    response = client.post(
        "/auth/email/register/code",
        json={"email": "User@Example.com"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "challenge_id": "challenge-register",
        "expires_in": 600,
        "resend_after": 60,
    }
    assert FakeEmailAuthService.calls == [
        ("register_code", "User@example.com", "192.0.2.10")
    ]


def test_registration_returns_tokens_and_validates_input(client) -> None:
    response = client.post(
        "/auth/email/register",
        json={
            "email": "user@example.com",
            "challenge_id": "challenge-register",
            "code": "012345",
            "password": "strong-password",
        },
    )
    invalid = client.post(
        "/auth/email/register",
        json={
            "email": "user@example.com",
            "challenge_id": "challenge-register",
            "code": "not-six",
            "password": "short",
            "role": "admin",
        },
    )

    assert response.status_code == 200
    assert response.json()["access_token"] == "access"
    assert response.json()["refresh_token"] == "refresh"
    assert invalid.status_code == 422


def test_email_login_uses_email_field_and_returns_uniform_401(client) -> None:
    success = client.post(
        "/auth/email/login",
        json={"email": "user@example.com", "password": "strong-password"},
    )
    FakeEmailAuthService.error = ValueError("missing user")
    failure = client.post(
        "/auth/email/login",
        json={"email": "missing@example.com", "password": "strong-password"},
    )

    assert success.status_code == 200
    assert failure.status_code == 401
    assert failure.json() == {"detail": "invalid credentials"}


def test_password_forgot_response_has_uniform_shape(client) -> None:
    response = client.post(
        "/auth/password/forgot/code",
        json={"email": "missing@example.com"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "message": "如果邮箱已注册，验证码将发送到该邮箱",
        "challenge_id": "challenge-reset",
        "expires_in": 600,
        "resend_after": 60,
    }


def test_password_reset_returns_message_without_tokens(client) -> None:
    response = client.post(
        "/auth/password/reset",
        json={
            "email": "user@example.com",
            "challenge_id": "challenge-reset",
            "code": "123456",
            "new_password": "replacement-password",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"message": "密码重置成功，请使用新密码登录"}
    assert "access_token" not in response.json()
    assert "refresh_token" not in response.json()


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/auth/email/register/code", {"email": "user@example.com"}),
        ("/auth/password/forgot/code", {"email": "user@example.com"}),
    ],
)
def test_code_routes_map_rate_limits_and_provider_failures(client, path: str, payload: dict) -> None:
    FakeEmailAuthService.error = ChallengeRateLimitError("limited")
    limited = client.post(path, json=payload)
    FakeEmailAuthService.error = EmailProviderError("secret provider detail")
    unavailable = client.post(path, json=payload)

    assert limited.status_code == 429
    assert limited.json() == {"detail": "verification send limit exceeded"}
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "email authentication unavailable"}
    assert "secret provider detail" not in unavailable.text


def test_register_maps_challenge_duplicate_and_configuration_errors(client) -> None:
    payload = {
        "email": "user@example.com",
        "challenge_id": "challenge-register",
        "code": "123456",
        "password": "strong-password",
    }
    FakeEmailAuthService.error = ChallengeCodeError("wrong code")
    invalid = client.post("/auth/email/register", json=payload)
    FakeEmailAuthService.error = EmailAlreadyRegisteredError("exists")
    duplicate = client.post("/auth/email/register", json=payload)
    FakeEmailAuthService.error = EmailAuthConfigurationError("missing role")
    unavailable = client.post("/auth/email/register", json=payload)

    assert invalid.status_code == 400
    assert invalid.json() == {"detail": "invalid verification challenge"}
    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "email registration failed"}
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "email authentication unavailable"}


def test_reset_maps_invalid_challenge_to_safe_400(client) -> None:
    FakeEmailAuthService.error = ChallengeCodeError("wrong code")

    response = client.post(
        "/auth/password/reset",
        json={
            "email": "user@example.com",
            "challenge_id": "challenge-reset",
            "code": "123456",
            "new_password": "replacement-password",
        },
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid verification challenge"}
