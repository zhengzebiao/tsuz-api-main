import httpx
import pytest

from app.clients.jcc_client import JccClient, JccClientAuthenticationError, JccClientRequestError
from app.core.config import settings


def test_jcc_client_uses_basic_only_for_token_and_caches_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "main_app_id", "app_main")
    monkeypatch.setattr(settings, "main_app_secret", "app_secret_example_value_123456")
    monkeypatch.setattr(settings, "jcc_app_id", "app_jcc")
    monkeypatch.setattr(settings, "main_token_url", "https://main.example/internal/oauth/token")
    monkeypatch.setattr(settings, "jcc_api_base_url", "https://jcc.example")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/internal/oauth/token":
            return httpx.Response(200, json={"access_token": "service-token", "expires_in": 300})
        return httpx.Response(200, json=[{"id": 1, "slug": "one"}])

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = JccClient(http_client)
        assert client.list_records(request_id="req-1")[0]["id"] == 1
        assert client.list_records()[0]["id"] == 1

    assert [request.url.path for request in requests].count("/internal/oauth/token") == 1
    assert requests[0].headers["Authorization"].startswith("Basic ")
    assert b"app_secret" not in requests[0].content
    assert all(request.headers["Authorization"] == "Bearer service-token" for request in requests[1:])
    assert requests[1].headers["X-Request-ID"] == "req-1"


def test_jcc_client_raises_safe_fixed_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "main_app_id", "app_main")
    monkeypatch.setattr(settings, "main_app_secret", "app_secret_example_value_123456")
    monkeypatch.setattr(settings, "jcc_app_id", "app_jcc")

    with (
        httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(401))) as http_client,
        pytest.raises(JccClientAuthenticationError) as exc_info,
    ):
        JccClient(http_client).list_records()
    assert str(exc_info.value) == "JCC_CLIENT_AUTHENTICATION_ERROR"
    assert settings.main_app_secret not in str(exc_info.value)

    responses = iter(
        [
            httpx.Response(200, json={"access_token": "service-token", "expires_in": 300}),
            httpx.Response(500),
        ]
    )
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _request: next(responses))) as http_client,
        pytest.raises(JccClientRequestError) as exc_info,
    ):
        JccClient(http_client).list_records()
    assert str(exc_info.value) == "JCC_CLIENT_REQUEST_ERROR"
