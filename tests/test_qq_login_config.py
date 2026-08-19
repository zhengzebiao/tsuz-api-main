from app.core.config import Settings


def test_qq_login_settings_default_urls() -> None:
    settings = Settings(_env_file=None)

    assert settings.qq_authorize_url == "https://graph.qq.com/oauth2.0/authorize"
    assert settings.qq_token_url == "https://graph.qq.com/oauth2.0/token"
    assert settings.qq_openid_url == "https://graph.qq.com/oauth2.0/me"
    assert settings.qq_user_info_url == "https://graph.qq.com/user/get_user_info"
    assert settings.qq_state_ttl_seconds == 300
    assert settings.qq_ticket_ttl_seconds == 60
    assert settings.qq_http_timeout_seconds == 10


def test_qq_login_settings_accept_app_credentials(monkeypatch) -> None:
    monkeypatch.setenv("APP_ID", "qq-app-id")
    monkeypatch.setenv("APP_KEY", "qq-app-key")
    monkeypatch.setenv("QQ_REDIRECT_URI", "https://api.example.test/auth/qq/callback")
    monkeypatch.setenv("QQ_TICKET_REDIRECT_URI", "https://example.test/login")

    settings = Settings(_env_file=None)

    assert settings.app_id == "qq-app-id"
    assert settings.app_key == "qq-app-key"
    assert settings.qq_redirect_uri == "https://api.example.test/auth/qq/callback"
    assert settings.qq_ticket_redirect_uri == "https://example.test/login"
