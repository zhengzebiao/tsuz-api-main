import re
from collections.abc import Sequence
from typing import Protocol, cast

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session as DbSession

from app.core.database import get_db
from app.models.user import User
from app.services.authorization_service import AuthenticationError, AuthorizationService, PermissionDeniedError

security = HTTPBearer(auto_error=False)

PERMISSION_NAME_MAX_LENGTH = 128
PERMISSION_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$")


class PermissionDeclarationError(ValueError):
    pass


class PermissionDependency(Protocol):
    required_permissions: tuple[str, ...]

    def __call__(
        self,
        credentials: HTTPAuthorizationCredentials | None = ...,
        db: DbSession = ...,
    ) -> User: ...


def validate_permission_names(permissions: Sequence[object]) -> tuple[str, ...]:
    if not permissions:
        raise PermissionDeclarationError("at least one permission is required")

    validated: list[str] = []
    seen: set[str] = set()
    for permission in permissions:
        if not isinstance(permission, str):
            raise PermissionDeclarationError("permission names must be strings")
        if permission != permission.strip():
            raise PermissionDeclarationError(f"permission name has surrounding whitespace: {permission!r}")
        if not permission:
            raise PermissionDeclarationError("permission name must not be empty")
        if len(permission) > PERMISSION_NAME_MAX_LENGTH:
            raise PermissionDeclarationError(
                f"permission name exceeds {PERMISSION_NAME_MAX_LENGTH} characters: {permission!r}"
            )
        if PERMISSION_NAME_PATTERN.fullmatch(permission) is None:
            raise PermissionDeclarationError(f"invalid permission name: {permission!r}")
        if permission in seen:
            raise PermissionDeclarationError(f"duplicate permission name: {permission!r}")
        validated.append(permission)
        seen.add(permission)

    return tuple(validated)


def require_access_token(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid access token")
    return credentials.credentials


def require_permissions(*permissions: str) -> PermissionDependency:
    validated_permissions = validate_permission_names(permissions)

    def dependency(
        credentials: HTTPAuthorizationCredentials | None = Depends(security),
        db: DbSession = Depends(get_db),
    ) -> User:
        access_token = require_access_token(credentials)
        try:
            return AuthorizationService(db).require_permissions(access_token, validated_permissions)
        except AuthenticationError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid access token") from exc
        except PermissionDeniedError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient permissions") from exc

    typed_dependency = cast(PermissionDependency, dependency)
    typed_dependency.required_permissions = validated_permissions
    return typed_dependency
