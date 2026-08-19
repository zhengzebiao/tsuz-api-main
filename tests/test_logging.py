import logging

from app.core.logging import redact_sensitive

RAW_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.signature"
RAW_PRIVATE_KEY = """-----BEGIN PRIVATE KEY-----
secret-key-material
-----END PRIVATE KEY-----"""


def test_request_id_is_echoed_and_attached_to_request_log(client, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="app.request"):
        response = client.get("/health", headers={"X-Request-ID": "req-test-123"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req-test-123"
    assert any(
        record.name == "app.request" and getattr(record, "request_id", "") == "req-test-123"
        for record in caplog.records
    )


def test_security_logs_redact_sensitive_values(caplog) -> None:
    logger = logging.getLogger("app.security.test")
    raw_refresh_token = "refresh-token-secret"
    raw_password = "correct-horse-battery-staple"
    raw_database_url = "postgresql+psycopg://test_user:test_password@localhost:5432/app"

    with caplog.at_level(logging.INFO, logger="app.security.test"):
        logger.info(
            "Authorization: Bearer %s access_token=%s refresh_token=%s password=%s db=%s key=%s",
            RAW_JWT,
            RAW_JWT,
            raw_refresh_token,
            raw_password,
            raw_database_url,
            RAW_PRIVATE_KEY,
        )

    assert RAW_JWT not in caplog.text
    assert raw_refresh_token not in caplog.text
    assert raw_password not in caplog.text
    assert "test_password" not in caplog.text
    assert "secret-key-material" not in caplog.text
    assert "[REDACTED" in caplog.text


def test_redact_sensitive_handles_json_and_env_style_secrets() -> None:
    message = (
        '{"password":"plain", "refresh_token":"refresh-secret"} '
        "JWT_PRIVATE_KEY=-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY----- "
        "redis://default:redis_password@localhost:6379/0"
    )

    redacted = redact_sensitive(message)

    assert "plain" not in redacted
    assert "refresh-secret" not in redacted
    assert "secret" not in redacted
    assert "redis_password" not in redacted
    assert "[REDACTED" in redacted


def test_redact_sensitive_handles_app_secrets_and_hashes() -> None:
    app_secret = "app_secret_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
    app_secret_hash = "a" * 64
    message = (
        f'app_secret={app_secret} app_secret_hash={app_secret_hash} '
        f'{{"app_secret":"{app_secret}", "app_secret_hash":"{app_secret_hash}"}} '
        f"unlabeled credential {app_secret}"
    )

    redacted = redact_sensitive(message)

    assert app_secret not in redacted
    assert app_secret_hash not in redacted
    assert "[REDACTED_APP_SECRET]" in redacted
    assert "app_secret=[REDACTED]" in redacted
    assert "app_secret_hash=[REDACTED]" in redacted


def test_redact_sensitive_handles_oauth_fields_without_overredacting() -> None:
    message = (
        "APP_KEY=app-key-value oauth_code:authorization-code qq_state=state-value "
        "qq_ticket=ticket-value openid=openid-value oauth_access_token=oauth-token "
        '{"qq_code":"json-code", "state":"json-state", "ticket":"json-ticket", '
        '"openid":"json-openid", "qq_access_token":"json-token"} '
        "code=ordinary-code id=ordinary-id"
    )

    redacted = redact_sensitive(message)

    for sensitive_value in (
        "app-key-value",
        "authorization-code",
        "state-value",
        "ticket-value",
        "openid-value",
        "oauth-token",
        "json-code",
        "json-state",
        "json-ticket",
        "json-openid",
        "json-token",
    ):
        assert sensitive_value not in redacted
    assert "code=ordinary-code" in redacted
    assert "id=ordinary-id" in redacted
