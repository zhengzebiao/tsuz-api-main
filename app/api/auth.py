import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session as DbSession

from app.core.database import get_db
from app.schemas.auth import LoginRequest, LogoutResponse, RefreshTokenRequest, TokenResponse, UserResponse
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])
security = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _require_access_token(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None:
        raise _unauthorized("invalid access token")
    return credentials.credentials


@router.post(
    "/login",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Login and issue tokens",
    responses={401: {"description": "Invalid credentials"}},
)
def login(payload: LoginRequest, db: DbSession = Depends(get_db)) -> TokenResponse:
    try:
        return AuthService(db).login(payload)
    except ValueError as exc:
        raise _unauthorized("invalid credentials") from exc


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Passively refresh access token",
    responses={401: {"description": "Invalid refresh token"}},
)
def refresh(payload: RefreshTokenRequest, db: DbSession = Depends(get_db)) -> TokenResponse:
    try:
        return AuthService(db).refresh(payload.refresh_token)
    except ValueError as exc:
        raise _unauthorized("invalid refresh token") from exc


@router.post(
    "/logout",
    response_model=LogoutResponse,
    summary="Logout current session and blacklist current access token jti",
    responses={401: {"description": "Invalid access token"}},
)
def logout(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: DbSession = Depends(get_db),
) -> LogoutResponse:
    try:
        return AuthService(db).logout(_require_access_token(credentials))
    except (jwt.PyJWTError, ValueError) as exc:
        raise _unauthorized("invalid access token") from exc


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Return current authenticated user",
    responses={401: {"description": "Invalid access token"}},
)
def me(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: DbSession = Depends(get_db),
) -> UserResponse:
    try:
        return AuthService(db).current_user(_require_access_token(credentials))
    except (jwt.PyJWTError, ValueError) as exc:
        raise _unauthorized("invalid access token") from exc
