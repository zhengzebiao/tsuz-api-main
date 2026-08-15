from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(_StrictModel):
    username: EmailStr
    password: str


class EmailRegistrationCodeRequest(_StrictModel):
    email: EmailStr


class EmailRegistrationRequest(_StrictModel):
    email: EmailStr
    challenge_id: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=6, max_length=6)
    password: str = Field(min_length=10, max_length=128)

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        if not value.isdigit():
            raise ValueError("code must contain only digits")
        return value


class EmailLoginRequest(_StrictModel):
    email: EmailStr
    password: str


class PasswordForgotCodeRequest(_StrictModel):
    email: EmailStr


class PasswordResetRequest(_StrictModel):
    email: EmailStr
    challenge_id: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=6, max_length=6)
    new_password: str = Field(min_length=10, max_length=128)

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        if not value.isdigit():
            raise ValueError("code must contain only digits")
        return value


class RefreshTokenRequest(_StrictModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int


class EmailChallengeResponse(_StrictModel):
    challenge_id: str
    expires_in: int
    resend_after: int


class PasswordForgotCodeResponse(_StrictModel):
    message: str
    challenge_id: str
    expires_in: int
    resend_after: int


class PasswordResetResponse(_StrictModel):
    message: str


class LogoutResponse(BaseModel):
    message: str


class UserResponse(BaseModel):
    id: str
    username: str
    roles: list[str] = []
