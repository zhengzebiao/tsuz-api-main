from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StrictInt, field_validator

from app.schemas.admin_role import AdminRoleSummary


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdminUserCreate(_StrictModel):
    email: EmailStr
    display_name: str | None = Field(default=None, max_length=128)
    password: str
    is_active: bool = True

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class AdminUserUpdate(_StrictModel):
    email: EmailStr | None = None
    display_name: str | None = Field(default=None, max_length=128)
    version: int = Field(gt=0)

    @field_validator("email")
    @classmethod
    def require_email_when_submitted(cls, value: EmailStr | None) -> EmailStr:
        if value is None:
            raise ValueError("email cannot be null")
        return value

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class UserStatusReason(_StrictModel):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        return value


class AdminPasswordReset(_StrictModel):
    new_password: str


class AdminForceLogoutRequest(_StrictModel):
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class AdminUserRoleAssignment(_StrictModel):
    role_ids: list[StrictInt]
    version: StrictInt = Field(gt=0)

    @field_validator("role_ids")
    @classmethod
    def validate_role_ids(cls, value: list[int]) -> list[int]:
        if any(role_id <= 0 for role_id in value):
            raise ValueError("role_ids must contain only positive integers")
        if len(value) != len(set(value)):
            raise ValueError("role_ids must not contain duplicates")
        return value


class AdminUserRolesResponse(_StrictModel):
    user_id: int
    roles: list[AdminRoleSummary]
    version: int
    changed: bool
    revoked_sessions: int = Field(default=0, ge=0)


class AdminUserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    display_name: str | None
    is_active: bool
    is_blacklisted: bool
    disabled_at: datetime | None
    disabled_reason: str | None
    blacklisted_at: datetime | None
    blacklisted_reason: str | None
    password_changed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int


class AdminUserActionResponse(AdminUserResponse):
    changed: bool
    revoked_sessions: int = 0


class AdminUserListResponse(BaseModel):
    items: list[AdminUserResponse]
    total: int
    page: int
    page_size: int


class AdminPasswordResetResponse(BaseModel):
    message: str
    revoked_sessions: int


class AdminForceLogoutResponse(BaseModel):
    message: str
    revoked_sessions: int
