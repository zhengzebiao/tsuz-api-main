import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

_PERMISSION_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$")
_MAX_PERMISSIONS = 500


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PermissionReportItem(_StrictModel):
    code: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=255)

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        if value != value.strip() or _PERMISSION_CODE_PATTERN.fullmatch(value) is None:
            raise ValueError("invalid permission code")
        return value

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("display_name must not be blank")
        return normalized

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        return value.strip()


class PermissionReportRequest(_StrictModel):
    permissions: list[PermissionReportItem] = Field(max_length=_MAX_PERMISSIONS)

    @field_validator("permissions")
    @classmethod
    def reject_duplicate_codes(cls, value: list[PermissionReportItem]) -> list[PermissionReportItem]:
        codes = [item.code for item in value]
        if len(codes) != len(set(codes)):
            raise ValueError("permission codes must not contain duplicates")
        return value


class PermissionReportResponse(_StrictModel):
    caller_app_id: str
    created: int = Field(ge=0)
    restored: int = Field(ge=0)
    marked_missing: int = Field(ge=0)
    admin_grants_added: int = Field(ge=0)
    sessions_revoked: int = Field(ge=0)
    unchanged: int = Field(ge=0)
