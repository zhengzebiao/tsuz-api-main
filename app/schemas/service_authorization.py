import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

_SCOPE_PATTERN = re.compile(
    r"^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$"
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def normalize_service_scope(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("scope_code must not be blank")
    if len(normalized) > 128 or _SCOPE_PATTERN.fullmatch(normalized) is None:
        raise ValueError("invalid service scope code")
    return normalized


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


class ResourceScopeCreate(_StrictModel):
    target_app_id: str = Field(min_length=1, max_length=64)
    scope_code: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=255)

    @field_validator("target_app_id")
    @classmethod
    def normalize_target_app_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("target_app_id must not be blank")
        return normalized

    @field_validator("scope_code")
    @classmethod
    def normalize_scope_code(cls, value: str) -> str:
        return normalize_service_scope(value)

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        return value.strip()


class ResourceScopeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: int
    target_app_id: str
    scope_code: str
    description: str
    is_enabled: bool
    created_at: datetime
    updated_at: datetime


class ResourceScopeListResponse(_StrictModel):
    items: list[ResourceScopeResponse]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)


class ResourceScopeActionResponse(ResourceScopeResponse):
    changed: bool


class AppServiceGrantCreate(_StrictModel):
    caller_app_id: str = Field(min_length=1, max_length=64)
    scope_id: StrictInt = Field(gt=0)
    valid_from: datetime | None = None
    expires_at: datetime | None = None

    @field_validator("caller_app_id")
    @classmethod
    def normalize_caller_app_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("caller_app_id must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_window(self) -> "AppServiceGrantCreate":
        if self.valid_from is not None and self.valid_from.tzinfo is not None:
            raise ValueError("valid_from must be timezone-naive UTC")
        if self.expires_at is not None and self.expires_at.tzinfo is not None:
            raise ValueError("expires_at must be timezone-naive UTC")
        if self.valid_from is not None and self.expires_at is not None and self.expires_at <= self.valid_from:
            raise ValueError("expires_at must be later than valid_from")
        return self


class AppServiceGrantRevokeRequest(_StrictModel):
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)


class AppServiceGrantResponse(_StrictModel):
    id: int
    caller_app_id: str
    scope_id: int
    target_app_id: str
    scope_code: str
    status: str
    valid_from: datetime
    expires_at: datetime | None
    created_by: int
    created_at: datetime
    revoked_by: int | None
    revoked_at: datetime | None
    revoke_reason: str | None


class AppServiceGrantListResponse(_StrictModel):
    items: list[AppServiceGrantResponse]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)


class AppServiceGrantActionResponse(AppServiceGrantResponse):
    changed: bool
