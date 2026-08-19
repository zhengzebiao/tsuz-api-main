from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_env: str = "test"
    debug: bool = True
    log_level: str = "debug"
    log_format: str = "json"
    request_id_header: str = "X-Request-ID"
    service_name: str = "auth-service"
    api_prefix: str = "/api"

    database_url: str = "postgresql+psycopg://test_user:test_password@localhost:5432/test_auth"
    db_sslmode: str = "disable"
    redis_url: str = "redis://localhost:6379/0"
    redis_key_prefix: str = "auth:test:"

    app_id: str = Field(default="", validation_alias="APP_ID")
    app_key: str = Field(default="", validation_alias="APP_KEY")
    qq_redirect_uri: str = ""
    qq_ticket_redirect_uri: str = ""
    qq_authorize_url: str = "https://graph.qq.com/oauth2.0/authorize"
    qq_token_url: str = "https://graph.qq.com/oauth2.0/token"
    qq_openid_url: str = "https://graph.qq.com/oauth2.0/me"
    qq_user_info_url: str = "https://graph.qq.com/user/get_user_info"
    qq_state_prefix: str = "auth:test:qq:state:"
    qq_ticket_prefix: str = "auth:test:qq:ticket:"
    qq_state_ttl_seconds: int = 300
    qq_ticket_ttl_seconds: int = 60
    qq_http_timeout_seconds: int = 10

    jwt_algorithm: str = "RS256"
    jwt_issuer: str = "auth-service-test"
    jwt_audience: str = "backend-api-test"
    jwt_private_key: str = ""
    jwt_public_key: str = ""

    access_token_expire_minutes: int = 60
    refresh_token_expire_days: int = 7
    refresh_token_rotate: bool = True
    refresh_token_reuse_grace_seconds: int = 10

    token_blacklist_prefix: str = "auth:test:blacklist:jti:"
    refresh_token_prefix: str = "auth:test:refresh:"
    session_prefix: str = "auth:test:session:"

    tencentcloud_secret_id: str = ""
    tencentcloud_secret_key: str = ""
    tencentcloud_region: str = "ap-guangzhou"
    tencentcloud_ses_endpoint: str = "ses.tencentcloudapi.com"

    email_from_address: str = "noreply@notify.tusz.online"
    email_from_name: str = "tusz.online"
    email_template_id: int = 57044
    email_subject: str = "邮箱验证码"
    email_code_expire_minutes: int = 10
    email_code_length: int = 6
    email_code_max_attempts: int = 5
    email_code_resend_interval_seconds: int = 60
    email_api_timeout_seconds: int = 10
    email_challenge_prefix: str = "auth:test:email:challenge:"
    email_send_limit_prefix: str = "auth:test:email:send:"
    email_ip_send_limit_prefix: str = "auth:test:email:ip-send:"
    email_send_limit_per_hour: int = 10
    email_ip_send_limit_per_minute: int = 5
    trusted_proxy_ips: str = "127.0.0.1,::1"

    cors_allow_origins: str = "http://localhost:5173"
    cors_allow_credentials: bool = True

    openapi_enabled: bool = True
    docs_enabled: bool = True
    redoc_enabled: bool = True

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allow_origins.split(",") if origin.strip()]


settings = Settings()
