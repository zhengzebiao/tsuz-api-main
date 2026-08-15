import pytest

from app.core.config import Settings


EMAIL_SETTING_ENV_VARS = (
    "TENCENTCLOUD_SECRET_ID",
    "TENCENTCLOUD_SECRET_KEY",
    "TENCENTCLOUD_REGION",
    "TENCENTCLOUD_SES_ENDPOINT",
    "EMAIL_FROM_ADDRESS",
    "EMAIL_FROM_NAME",
    "EMAIL_TEMPLATE_ID",
    "EMAIL_SUBJECT",
    "EMAIL_CODE_EXPIRE_MINUTES",
    "EMAIL_CODE_LENGTH",
    "EMAIL_CODE_MAX_ATTEMPTS",
    "EMAIL_CODE_RESEND_INTERVAL_SECONDS",
    "EMAIL_API_TIMEOUT_SECONDS",
    "EMAIL_CHALLENGE_PREFIX",
    "EMAIL_SEND_LIMIT_PREFIX",
    "EMAIL_IP_SEND_LIMIT_PREFIX",
    "EMAIL_SEND_LIMIT_PER_HOUR",
    "EMAIL_IP_SEND_LIMIT_PER_MINUTE",
)


@pytest.fixture
def email_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name in EMAIL_SETTING_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None)


def test_email_registration_settings_use_confirmed_defaults(email_settings: Settings) -> None:
    assert email_settings.tencentcloud_secret_id == ""
    assert email_settings.tencentcloud_secret_key == ""
    assert email_settings.tencentcloud_region == "ap-guangzhou"
    assert email_settings.tencentcloud_ses_endpoint == "ses.tencentcloudapi.com"
    assert email_settings.email_from_address == "noreply@notify.tusz.online"
    assert email_settings.email_from_name == "tusz.online"
    assert email_settings.email_template_id == 57044
    assert email_settings.email_subject == "邮箱验证码"
    assert email_settings.email_code_expire_minutes == 10
    assert email_settings.email_code_length == 6
    assert email_settings.email_code_max_attempts == 5
    assert email_settings.email_code_resend_interval_seconds == 60
    assert email_settings.email_api_timeout_seconds == 10
    assert email_settings.email_challenge_prefix == "auth:test:email:challenge:"
    assert email_settings.email_send_limit_prefix == "auth:test:email:send:"
    assert email_settings.email_ip_send_limit_prefix == "auth:test:email:ip-send:"
    assert email_settings.email_send_limit_per_hour == 10
    assert email_settings.email_ip_send_limit_per_minute == 5


def test_email_registration_numeric_settings_are_integers(email_settings: Settings) -> None:
    numeric_values = (
        email_settings.email_template_id,
        email_settings.email_code_expire_minutes,
        email_settings.email_code_length,
        email_settings.email_code_max_attempts,
        email_settings.email_code_resend_interval_seconds,
        email_settings.email_api_timeout_seconds,
        email_settings.email_send_limit_per_hour,
        email_settings.email_ip_send_limit_per_minute,
    )

    assert all(isinstance(value, int) for value in numeric_values)
