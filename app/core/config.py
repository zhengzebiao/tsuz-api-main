from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

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

    cors_allow_origins: str = "http://localhost:5173"
    cors_allow_credentials: bool = True

    openapi_enabled: bool = True
    docs_enabled: bool = True
    redoc_enabled: bool = True

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allow_origins.split(",") if origin.strip()]


settings = Settings()
