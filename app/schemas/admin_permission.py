from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _normalize_required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    return normalized


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


class AdminPermissionUpdate(_StrictModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=255)
    version: StrictInt = Field(gt=0)

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("display_name cannot be null")
        return _normalize_required_text(value, "display_name")

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("description cannot be null")
        return value.strip()


class AdminPermissionDisableRequest(_StrictModel):
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)


class AdminPermissionEndpointResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    http_method: str
    path: str
    route_name: str


class AdminPermissionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    display_name: str
    description: str
    resource: str
    action: str
    is_declared: bool
    is_enabled: bool
    disabled_at: datetime | None
    disabled_reason: str | None
    missing_at: datetime | None
    endpoint_count: int = Field(ge=0)
    role_count: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime
    version: int


class AdminPermissionDetailResponse(AdminPermissionResponse):
    endpoints: list[AdminPermissionEndpointResponse]


class AdminPermissionListResponse(_StrictModel):
    items: list[AdminPermissionResponse]
    total: int
    page: int
    page_size: int


class AdminPermissionActionResponse(AdminPermissionResponse):
    changed: bool
    revoked_sessions: int = Field(default=0, ge=0)
