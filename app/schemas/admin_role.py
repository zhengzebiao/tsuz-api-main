from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator


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


class AdminRoleCreate(_StrictModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=255)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return _normalize_required_text(value, "name")

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        return value.strip()


class AdminRoleUpdate(_StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=255)
    version: StrictInt = Field(gt=0)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("name cannot be null")
        return _normalize_required_text(value, "name")

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("description cannot be null")
        return value.strip()


class AdminRoleDisableRequest(_StrictModel):
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)


class AdminRoleSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: int
    name: str
    description: str
    is_enabled: bool


class AdminRoleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: int
    name: str
    description: str
    is_enabled: bool
    disabled_at: datetime | None
    disabled_reason: str | None
    created_at: datetime
    updated_at: datetime
    version: int


class AdminRoleListResponse(_StrictModel):
    items: list[AdminRoleResponse]
    total: int
    page: int
    page_size: int


class AdminRoleActionResponse(AdminRoleResponse):
    changed: bool
    revoked_sessions: int = Field(default=0, ge=0)
