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
