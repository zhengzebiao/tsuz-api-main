from datetime import datetime

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _normalize_required_text(value: str, field_name: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field_name} must not be blank")
    return value


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _normalize_url(value: AnyHttpUrl | None) -> str | None:
    if value is None:
        return None
    return str(value)


class AdminAppCreate(_StrictModel):
    name: str = Field(min_length=1, max_length=128)
    icon_url: AnyHttpUrl | None = Field(default=None, max_length=2048)
    access_url: AnyHttpUrl = Field(max_length=2048)
    service_account_name: str = Field(min_length=1, max_length=128)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return _normalize_required_text(value, "name")

    @field_validator("icon_url", "access_url")
    @classmethod
    def normalize_urls(cls, value: AnyHttpUrl | None) -> str | None:
        return _normalize_url(value)

    @field_validator("service_account_name")
    @classmethod
    def normalize_service_account_name(cls, value: str) -> str:
        return _normalize_required_text(value, "service_account_name")


class AdminAppUpdate(_StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    icon_url: AnyHttpUrl | None = Field(default=None, max_length=2048)
    access_url: AnyHttpUrl | None = Field(default=None, max_length=2048)
    service_account_name: str | None = Field(default=None, min_length=1, max_length=128)
    version: int = Field(gt=0)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("name cannot be null")
        return _normalize_required_text(value, "name")

    @field_validator("icon_url")
    @classmethod
    def normalize_icon_url(cls, value: AnyHttpUrl | None) -> str | None:
        return _normalize_url(value)

    @field_validator("access_url")
    @classmethod
    def normalize_access_url(cls, value: AnyHttpUrl | None) -> str:
        if value is None:
            raise ValueError("access_url cannot be null")
        return str(value)

    @field_validator("service_account_name")
    @classmethod
    def normalize_service_account_name(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("service_account_name cannot be null")
        return _normalize_required_text(value, "service_account_name")


class AdminAppDisableRequest(_StrictModel):
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)


class AdminAppRegenerateSecretRequest(_StrictModel):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        return _normalize_required_text(value, "reason")


class AdminAppResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: int
    app_id: str
    name: str
    icon_url: str | None
    access_url: str
    service_account_name: str
    is_enabled: bool
    disabled_at: datetime | None
    disabled_reason: str | None
    secret_updated_at: datetime
    created_at: datetime
    updated_at: datetime
    version: int


class AdminAppListResponse(_StrictModel):
    items: list[AdminAppResponse]
    total: int
    page: int
    page_size: int


class AdminAppActionResponse(AdminAppResponse):
    changed: bool


class AdminAppCreateResponse(_StrictModel):
    app: AdminAppResponse
    app_secret: str


class AdminAppSecretResponse(_StrictModel):
    app_id: str
    app_secret: str
    secret_updated_at: datetime
