from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.service_authorization import normalize_service_scope


class ServiceTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant_type: str
    audience: str = Field(min_length=1, max_length=64)
    scope: str = Field(min_length=1, max_length=4096)

    @field_validator("grant_type")
    @classmethod
    def validate_grant_type(cls, value: str) -> str:
        if value != "client_credentials":
            raise ValueError("unsupported grant_type")
        return value

    @field_validator("audience")
    @classmethod
    def normalize_audience(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("audience must not be blank")
        return normalized

    @field_validator("scope")
    @classmethod
    def normalize_scope(cls, value: str) -> str:
        raw_scopes = value.split()
        if not raw_scopes:
            raise ValueError("scope must not be blank")
        if len(raw_scopes) != len(set(raw_scopes)):
            raise ValueError("scope must not contain duplicates")
        scopes = [normalize_service_scope(scope) for scope in raw_scopes]
        return " ".join(sorted(scopes))

    @property
    def requested_scopes(self) -> set[str]:
        return set(self.scope.split())


class ServiceTokenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    scope: str


class OAuthErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: str
    error_description: str | None = None
