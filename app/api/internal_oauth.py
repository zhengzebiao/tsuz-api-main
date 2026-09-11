import logging

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import ValidationError
from sqlalchemy.orm import Session as DbSession
from starlette.datastructures import FormData

from app.core.database import get_db
from app.schemas.service_token import ServiceTokenRequest, ServiceTokenResponse
from app.services.service_token_service import (
    InvalidClientError,
    InvalidScopeError,
    InvalidTargetError,
    ServiceTokenConfigurationError,
    ServiceTokenService,
)

router = APIRouter(prefix="/internal/oauth", tags=["internal-oauth"])
security = HTTPBasic(
    auto_error=False,
    scheme_name="ServiceClientBasic",
    description="HTTP Basic credentials where username is the app_id and password is the app_secret.",
)
_BASIC_DEPENDENCY = Depends(security)
_DB_DEPENDENCY = Depends(get_db)
logger = logging.getLogger("app.service_auth")

_SERVICE_TOKEN_FORM_SCHEMA = ServiceTokenRequest.model_json_schema()
_SERVICE_TOKEN_OPENAPI_EXTRA = {
    "requestBody": {
        "required": True,
        "content": {
            "application/x-www-form-urlencoded": {
                "schema": _SERVICE_TOKEN_FORM_SCHEMA,
            }
        },
    }
}


def _error_response(
    error: str,
    status_code: int,
    description: str | None = None,
) -> JSONResponse:
    content: dict[str, str] = {"error": error}
    if description is not None:
        content["error_description"] = description
    response = JSONResponse(content, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


def _form_payload(form: FormData) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in form.multi_items():
        if key in payload:
            raise ValueError("duplicate form field")
        payload[key] = value
    return payload


@router.post(
    "/token",
    response_model=ServiceTokenResponse,
    responses={
        400: {"description": "Invalid service token request"},
        401: {"description": "Invalid client credentials"},
        503: {"description": "Service token configuration unavailable"},
    },
    summary="Issue an application service token",
    openapi_extra=_SERVICE_TOKEN_OPENAPI_EXTRA,
)
async def issue_service_token(
    request: Request,
    credentials: HTTPBasicCredentials | None = _BASIC_DEPENDENCY,
    db: DbSession = _DB_DEPENDENCY,
) -> ServiceTokenResponse | JSONResponse:
    if credentials is None:
        logger.warning("service token rejected reason=invalid_client")
        return _error_response("invalid_client", status.HTTP_401_UNAUTHORIZED)
    try:
        form = await request.form()
        token_request = ServiceTokenRequest.model_validate(_form_payload(form))
    except (ValidationError, ValueError):
        logger.warning("service token rejected reason=invalid_request")
        return _error_response(
            "invalid_request",
            status.HTTP_400_BAD_REQUEST,
            "Invalid service token request",
        )

    service = ServiceTokenService(db)
    try:
        caller = service.authenticate_client(credentials.username, credentials.password)
        token, scope = service.issue_token(
            caller=caller,
            audience=token_request.audience,
            requested_scopes=token_request.requested_scopes,
        )
    except InvalidClientError:
        logger.warning("service token rejected reason=invalid_client")
        return _error_response("invalid_client", status.HTTP_401_UNAUTHORIZED)
    except InvalidTargetError:
        logger.warning("service token rejected reason=invalid_target")
        return _error_response(
            "invalid_target",
            status.HTTP_400_BAD_REQUEST,
            "Requested audience is unavailable",
        )
    except InvalidScopeError:
        logger.warning("service token rejected reason=invalid_scope")
        return _error_response(
            "invalid_scope",
            status.HTTP_400_BAD_REQUEST,
            "Requested scope is not granted",
        )
    except ServiceTokenConfigurationError:
        logger.error("service token rejected reason=configuration_error")
        return _error_response("server_error", status.HTTP_503_SERVICE_UNAVAILABLE)

    response = JSONResponse(
        ServiceTokenResponse(
            access_token=token,
            expires_in=service.expires_in_seconds,
            scope=scope,
        ).model_dump(),
        status_code=status.HTTP_200_OK,
    )
    response.headers["Cache-Control"] = "no-store"
    return response
