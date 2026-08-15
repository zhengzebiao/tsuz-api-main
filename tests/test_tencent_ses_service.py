import json
import logging
from types import SimpleNamespace

import pytest

import app.services.tencent_ses_service as ses_module
from app.core.config import Settings
from app.services.tencent_ses_service import EmailProviderError, TencentSesService


class RecordingSesClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.requests = []

    def SendEmail(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(MessageId="message-123", RequestId="request-123")


@pytest.fixture
def email_settings() -> Settings:
    return Settings(
        _env_file=None,
        tencentcloud_secret_id="secret-id-value",
        tencentcloud_secret_key="secret-key-value",
        tencentcloud_region="ap-guangzhou",
        tencentcloud_ses_endpoint="ses.tencentcloudapi.com",
        email_from_address="noreply@notify.tusz.online",
        email_from_name="tusz.online",
        email_template_id=57044,
        email_subject="邮箱验证码",
        email_code_expire_minutes=10,
        email_api_timeout_seconds=10,
    )


def test_send_verification_email_builds_confirmed_ses_request(email_settings: Settings) -> None:
    client = RecordingSesClient()
    service = TencentSesService(email_settings, client=client)

    result = service.send_verification_email(
        "user@example.com",
        "012345",
        purpose="register",
    )

    assert result.message_id == "message-123"
    assert result.request_id == "request-123"
    assert len(client.requests) == 1
    request = client.requests[0]
    assert request.FromEmailAddress == "tusz.online <noreply@notify.tusz.online>"
    assert request.Destination == ["user@example.com"]
    assert request.Subject == "邮箱验证码"
    assert request.TriggerType == 1
    assert request.Template.TemplateID == 57044
    assert request.Template.TemplateData == '{"code":"012345","expire_minutes":10}'
    template_data = json.loads(request.Template.TemplateData)
    assert template_data["code"] == "012345"
    assert isinstance(template_data["code"], str)
    assert template_data["expire_minutes"] == 10
    assert isinstance(template_data["expire_minutes"], int)


def test_build_client_uses_credentials_region_endpoint_and_timeout(
    email_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeHttpProfile:
        def __init__(self, *, endpoint: str, reqTimeout: int) -> None:
            captured["endpoint"] = endpoint
            captured["timeout"] = reqTimeout

    class FakeClientProfile:
        def __init__(self, *, httpProfile) -> None:
            captured["http_profile"] = httpProfile

    class FakeCredential:
        def __init__(self, secret_id: str, secret_key: str) -> None:
            captured["secret_id"] = secret_id
            captured["secret_key"] = secret_key

    class FakeSesClient:
        def __init__(self, credential, region: str, profile) -> None:
            captured["credential"] = credential
            captured["region"] = region
            captured["profile"] = profile

    monkeypatch.setattr(ses_module, "HttpProfile", FakeHttpProfile)
    monkeypatch.setattr(ses_module, "ClientProfile", FakeClientProfile)
    monkeypatch.setattr(ses_module.credential, "Credential", FakeCredential)
    monkeypatch.setattr(ses_module.ses_client, "SesClient", FakeSesClient)

    TencentSesService(email_settings)

    assert captured["secret_id"] == "secret-id-value"
    assert captured["secret_key"] == "secret-key-value"
    assert captured["region"] == "ap-guangzhou"
    assert captured["endpoint"] == "ses.tencentcloudapi.com"
    assert captured["timeout"] == 10


def test_missing_cam_credentials_fail_closed() -> None:
    configured_settings = Settings(
        _env_file=None,
        tencentcloud_secret_id="",
        tencentcloud_secret_key="",
    )

    with pytest.raises(EmailProviderError, match="not configured"):
        TencentSesService(configured_settings)


def test_sdk_failure_is_converted_without_leaking_sensitive_data(
    email_settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    code = "918273"
    secret = "secret-key-value"
    recipient = "private.recipient@example.com"
    client = RecordingSesClient(error=RuntimeError(f"provider failure code={code} secret={secret}"))
    service = TencentSesService(email_settings, client=client)

    with caplog.at_level(logging.ERROR, logger="app.auth.email"):
        with pytest.raises(EmailProviderError, match="email provider unavailable") as exc_info:
            service.send_verification_email(recipient, code, purpose="password_reset")

    assert recipient not in caplog.text
    assert code not in caplog.text
    assert secret not in caplog.text
    assert code not in str(exc_info.value)
    assert secret not in str(exc_info.value)
    assert "p***@e***" in caplog.text
    assert "purpose=password_reset" in caplog.text


def test_success_log_contains_request_id_but_not_code_or_full_recipient(
    email_settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = RecordingSesClient()
    service = TencentSesService(email_settings, client=client)

    with caplog.at_level(logging.INFO, logger="app.auth.email"):
        service.send_verification_email("person@example.com", "123456", purpose="register")

    assert "request_id=request-123" in caplog.text
    assert "person@example.com" not in caplog.text
    assert "123456" not in caplog.text
