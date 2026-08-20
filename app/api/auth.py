# ruff: noqa: B008

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DbSession

from app.api.client_ip import get_client_ip
from app.api.dependencies import require_access_token, security
from app.core.config import settings
from app.core.database import get_db
from app.schemas.auth import (
    EmailChallengeResponse,
    EmailLoginRequest,
    EmailRegistrationCodeRequest,
    EmailRegistrationRequest,
    LoginRequest,
    LogoutResponse,
    PasswordForgotCodeRequest,
    PasswordForgotCodeResponse,
    PasswordResetRequest,
    PasswordResetResponse,
    QQTicketExchangeRequest,
    RefreshTokenRequest,
    TokenResponse,
    UserResponse,
)
from app.services.auth_service import AuthService
from app.services.authorization_service import AuthenticationError
from app.services.email_auth_service import (
    EmailAlreadyRegisteredError,
    EmailAuthConfigurationError,
    EmailAuthService,
    EmailAuthStateError,
    EmailPasswordPolicyError,
)
from app.services.qq_oauth_service import (
    QQOAuthConfigurationError,
    QQOAuthIdentityError,
    QQOAuthProviderError,
    QQOAuthService,
    QQOAuthStateError,
    QQOAuthTicketError,
)
from app.services.tencent_ses_service import EmailProviderError
from app.services.verification_challenge_service import (
    ChallengeError,
    ChallengeRateLimitError,
    ChallengeStateError,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _email_service_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="email authentication unavailable",
    )


def _qq_service_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="QQ authentication unavailable",
    )


def _qq_error_redirect() -> RedirectResponse:
    target = _append_query_parameter(settings.qq_ticket_redirect_uri, "qq_error", "oauth_failed")
    return RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)


def _append_query_parameter(url: str, key: str, value: str) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _validate_qq_ticket_redirect_uri() -> None:
    if not settings.qq_ticket_redirect_uri.strip():
        raise _qq_service_unavailable()


@router.get(
    "/qq/login",
    status_code=status.HTTP_302_FOUND,
    summary="Start QQ OAuth login",
)
def qq_login(db: DbSession = Depends(get_db)) -> RedirectResponse:
    try:
        return RedirectResponse(
            url=QQOAuthService(db).build_authorize_url(),
            status_code=status.HTTP_302_FOUND,
        )
    except (QQOAuthConfigurationError, QQOAuthStateError) as exc:
        raise _qq_service_unavailable() from exc


@router.get(
    "/qq/callback",
    status_code=status.HTTP_302_FOUND,
    summary="Complete QQ OAuth login",
)
def qq_callback(
    code: str | None = None,
    state: str | None = None,
    db: DbSession = Depends(get_db),
) -> RedirectResponse:
    _validate_qq_ticket_redirect_uri()
    if code is None or state is None:
        return _qq_error_redirect()
    try:
        ticket = QQOAuthService(db).complete_authorization(code, state)
    except (
        QQOAuthConfigurationError,
        QQOAuthStateError,
        QQOAuthProviderError,
        QQOAuthIdentityError,
        QQOAuthTicketError,
        RedisError,
        SQLAlchemyError,
        AuthenticationError,
    ):
        return _qq_error_redirect()
    return RedirectResponse(
        url=_append_query_parameter(settings.qq_ticket_redirect_uri, "ticket", ticket),
        status_code=status.HTTP_302_FOUND,
    )


@router.post(
    "/qq/exchange",
    response_model=TokenResponse,
    summary="Exchange a QQ login ticket for tokens",
    responses={401: {"description": "Invalid QQ ticket"}},
)
def qq_exchange(
    payload: QQTicketExchangeRequest,
    db: DbSession = Depends(get_db),
) -> TokenResponse:
    try:
        user_id = QQOAuthService(db).consume_ticket(payload.ticket)
        return AuthService(db).complete_login(user_id)
    except (QQOAuthConfigurationError, QQOAuthIdentityError, RedisError, SQLAlchemyError) as exc:
        raise _qq_service_unavailable() from exc
    except (QQOAuthTicketError, AuthenticationError) as exc:
        raise _unauthorized("invalid QQ ticket") from exc


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
    "/email/register/code",
    response_model=EmailChallengeResponse,
    summary="Send an email registration verification code",
)
def send_registration_code(
    payload: EmailRegistrationCodeRequest,
    request: Request,
    db: DbSession = Depends(get_db),
) -> EmailChallengeResponse:
    try:
        return EmailAuthService(db).send_registration_code(
            str(payload.email),
            get_client_ip(request),
        )
    except ChallengeRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="verification send limit exceeded",
        ) from exc
    except (EmailProviderError, ChallengeStateError, EmailAuthStateError) as exc:
        raise _email_service_unavailable() from exc


@router.post(
    "/email/register",
    response_model=TokenResponse,
    summary="Register with an email verification code",
)
def register_with_email(
    payload: EmailRegistrationRequest,
    db: DbSession = Depends(get_db),
) -> TokenResponse:
    try:
        return EmailAuthService(db).register(
            str(payload.email),
            payload.challenge_id,
            payload.code,
            payload.password,
        )
    except ChallengeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid verification challenge",
        ) from exc
    except EmailAlreadyRegisteredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="email registration failed",
        ) from exc
    except EmailPasswordPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid password",
        ) from exc
    except (ChallengeStateError, EmailAuthConfigurationError, EmailAuthStateError) as exc:
        raise _email_service_unavailable() from exc


@router.post(
    "/email/login",
    response_model=TokenResponse,
    summary="Login with email and password",
    responses={401: {"description": "Invalid credentials"}},
)
def login_with_email(
    payload: EmailLoginRequest,
    db: DbSession = Depends(get_db),
) -> TokenResponse:
    try:
        return EmailAuthService(db).login(str(payload.email), payload.password)
    except ValueError as exc:
        raise _unauthorized("invalid credentials") from exc


@router.post(
    "/password/forgot/code",
    response_model=PasswordForgotCodeResponse,
    summary="Send a password reset verification code",
)
def send_password_reset_code(
    payload: PasswordForgotCodeRequest,
    request: Request,
    db: DbSession = Depends(get_db),
) -> PasswordForgotCodeResponse:
    try:
        return EmailAuthService(db).send_password_reset_code(
            str(payload.email),
            get_client_ip(request),
        )
    except ChallengeRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="verification send limit exceeded",
        ) from exc
    except (EmailProviderError, ChallengeStateError, EmailAuthStateError) as exc:
        raise _email_service_unavailable() from exc


@router.post(
    "/password/reset",
    response_model=PasswordResetResponse,
    summary="Reset a password with an email verification code",
)
def reset_password(
    payload: PasswordResetRequest,
    db: DbSession = Depends(get_db),
) -> PasswordResetResponse:
    try:
        return EmailAuthService(db).reset_password(
            str(payload.email),
            payload.challenge_id,
            payload.code,
            payload.new_password,
        )
    except ChallengeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid verification challenge",
        ) from exc
    except EmailPasswordPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid password",
        ) from exc
    except EmailAuthStateError as exc:
        raise _email_service_unavailable() from exc


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
        return AuthService(db).logout(require_access_token(credentials))
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
        return AuthService(db).current_user(require_access_token(credentials))
    except (jwt.PyJWTError, ValueError) as exc:
        raise _unauthorized("invalid access token") from exc
