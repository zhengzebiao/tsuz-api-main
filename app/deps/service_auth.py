import logging
from collections.abc import Callable
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings
from app.core.security import parse_pem_key

service_security = HTTPBearer(auto_error=False, scheme_name="ServiceBearer")
_SERVICE_BEARER_DEPENDENCY = Security(service_security)
logger = logging.getLogger("app.service_auth")


@dataclass(frozen=True)
class ServicePrincipal:
    caller_app_id: str
    audience: str
    jti: str
    scope: str

    @property
    def scopes(self) -> set[str]:
        return set(self.scope.split())


def get_service_principal(
    credentials: HTTPAuthorizationCredentials | None = _SERVICE_BEARER_DEPENDENCY,
) -> ServicePrincipal:
    if credentials is None:
        _reject("missing_token")
    try:
        if not settings.service_token_public_key or not settings.main_app_id:
            raise ValueError("service token verification is not configured")
        payload = jwt.decode(
            credentials.credentials,
            parse_pem_key(settings.service_token_public_key),
            algorithms=[settings.jwt_algorithm],
            issuer=settings.service_token_issuer,
            audience=settings.main_app_id,
            leeway=settings.service_token_clock_skew_seconds,
            options={
                "require": ["iss", "sub", "aud", "token_use", "scope", "iat", "nbf", "exp", "jti"],
                "strict_aud": True,
            },
        )
        caller_app_id = _required_string_claim(payload, "sub")
        audience = _required_string_claim(payload, "aud")
        if audience != settings.main_app_id:
            raise ValueError("invalid audience claim")
        if _required_string_claim(payload, "token_use") != "service":
            raise ValueError("invalid token_use claim")
        scope = _scope_claim(payload)
        jti = _required_string_claim(payload, "jti")
        _numeric_claim(payload, "iat")
        _numeric_claim(payload, "nbf")
        _numeric_claim(payload, "exp")
    except (jwt.PyJWTError, TypeError, ValueError):
        _reject("invalid_token")
    return ServicePrincipal(
        caller_app_id=caller_app_id,
        audience=audience,
        jti=jti,
        scope=scope,
    )


def require_service_scope(required_scope: str) -> Callable[[ServicePrincipal], ServicePrincipal]:
    principal_dependency = Depends(get_service_principal)

    def dependency(principal: ServicePrincipal = principal_dependency) -> ServicePrincipal:
        if required_scope not in principal.scopes:
            logger.warning(
                "service authorization rejected reason=insufficient_scope caller_app_id=%s required_scope=%s",
                principal.caller_app_id,
                required_scope,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="insufficient_scope",
            )
        return principal

    return dependency


def _reject(reason: str) -> None:
    logger.warning("service auth rejected reason=%s", reason)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid service token")


def _required_string_claim(payload: dict, claim: str) -> str:
    value = payload.get(claim)
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid {claim} claim")
    return value


def _scope_claim(payload: dict) -> str:
    value = _required_string_claim(payload, "scope")
    scopes = value.split()
    if len(scopes) != len(set(scopes)):
        raise ValueError("duplicate scopes")
    return value


def _numeric_claim(payload: dict, claim: str) -> int | float:
    value = payload.get(claim)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"invalid {claim} claim")
    return value
