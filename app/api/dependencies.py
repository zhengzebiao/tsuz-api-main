from collections.abc import Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session as DbSession

from app.core.database import get_db
from app.models.user import User
from app.services.authorization_service import AuthenticationError, AuthorizationService, PermissionDeniedError

security = HTTPBearer(auto_error=False)


def require_access_token(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid access token")
    return credentials.credentials


def require_permissions(*permissions: str) -> Callable[..., User]:
    def dependency(
        credentials: HTTPAuthorizationCredentials | None = Depends(security),
        db: DbSession = Depends(get_db),
    ) -> User:
        access_token = require_access_token(credentials)
        try:
            return AuthorizationService(db).require_permissions(access_token, permissions)
        except AuthenticationError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid access token") from exc
        except PermissionDeniedError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient permissions") from exc

    return dependency
