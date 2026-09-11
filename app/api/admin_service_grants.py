from collections.abc import Callable
from typing import TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session as DbSession

from app.api.dependencies import require_permissions
from app.core.database import get_db
from app.core.logging import request_id_context
from app.models.user import User
from app.schemas.service_authorization import (
    AppServiceGrantActionResponse,
    AppServiceGrantCreate,
    AppServiceGrantListResponse,
    AppServiceGrantResponse,
    AppServiceGrantRevokeRequest,
)
from app.services.app_service_grant_service import (
    AppServiceGrantAlreadyExistsError,
    AppServiceGrantCallerNotFoundError,
    AppServiceGrantError,
    AppServiceGrantNotFoundError,
    AppServiceGrantRecord,
    AppServiceGrantRevokedError,
    AppServiceGrantScopeNotFoundError,
    AppServiceGrantService,
)

router = APIRouter(prefix="/admin/service-grants", tags=["admin-service-grants"])

_GRANT_READ_DEPENDENCY = Depends(require_permissions("service_grant:read"))
_GRANT_CREATE_DEPENDENCY = Depends(require_permissions("service_grant:create"))
_GRANT_REVOKE_DEPENDENCY = Depends(require_permissions("service_grant:revoke"))
_DB_DEPENDENCY = Depends(get_db)

_ERROR_STATUS_CODES: dict[type[AppServiceGrantError], int] = {
    AppServiceGrantNotFoundError: status.HTTP_404_NOT_FOUND,
    AppServiceGrantCallerNotFoundError: status.HTTP_404_NOT_FOUND,
    AppServiceGrantScopeNotFoundError: status.HTTP_404_NOT_FOUND,
    AppServiceGrantAlreadyExistsError: status.HTTP_409_CONFLICT,
    AppServiceGrantRevokedError: status.HTTP_409_CONFLICT,
}
_ResponseModel = TypeVar("_ResponseModel", bound=BaseModel)


def _grant_response(record: AppServiceGrantRecord) -> AppServiceGrantResponse:
    grant = record.grant
    return AppServiceGrantResponse(
        id=grant.id,
        caller_app_id=grant.caller_app_id,
        scope_id=grant.scope_id,
        target_app_id=record.target_app_id,
        scope_code=record.scope_code,
        status=grant.status,
        valid_from=grant.valid_from,
        expires_at=grant.expires_at,
        created_by=grant.created_by,
        created_at=grant.created_at,
        revoked_by=grant.revoked_by,
        revoked_at=grant.revoked_at,
        revoke_reason=grant.revoke_reason,
    )


def _action_response(record: AppServiceGrantRecord, changed: bool) -> AppServiceGrantActionResponse:
    return AppServiceGrantActionResponse(**_grant_response(record).model_dump(), changed=changed)


def _raise_admin_error(exc: AppServiceGrantError) -> None:
    status_code = next(
        (code for error_type, code in _ERROR_STATUS_CODES.items() if isinstance(exc, error_type)),
        status.HTTP_400_BAD_REQUEST,
    )
    raise HTTPException(status_code=status_code, detail=getattr(exc, "code", AppServiceGrantError.code)) from exc


def _execute_write(db: DbSession, operation: Callable[[], _ResponseModel]) -> _ResponseModel:
    try:
        response = operation()
        db.commit()
        return response
    except AppServiceGrantError as exc:
        db.rollback()
        _raise_admin_error(exc)
    except Exception:
        db.rollback()
        raise
    raise AssertionError("unreachable")


@router.get("", response_model=AppServiceGrantListResponse, summary="List service grants")
def list_service_grants(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    caller_app_id: str | None = Query(default=None, max_length=64),
    target_app_id: str | None = Query(default=None, max_length=64),
    grant_status: str | None = Query(default=None, alias="status", pattern=r"^(enabled|revoked)$"),
    _actor: User = _GRANT_READ_DEPENDENCY,
    db: DbSession = _DB_DEPENDENCY,
) -> AppServiceGrantListResponse:
    grants, total = AppServiceGrantService(db).list_grants(
        page=page,
        page_size=page_size,
        caller_app_id=caller_app_id,
        target_app_id=target_app_id,
        status=grant_status,
    )
    return AppServiceGrantListResponse(
        items=[_grant_response(record) for record in grants],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "",
    response_model=AppServiceGrantActionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a service grant",
)
def create_service_grant(
    payload: AppServiceGrantCreate,
    _request: Request,
    actor: User = _GRANT_CREATE_DEPENDENCY,
    db: DbSession = _DB_DEPENDENCY,
) -> AppServiceGrantActionResponse:
    def operation() -> AppServiceGrantActionResponse:
        record, changed = AppServiceGrantService(db).create_grant(
            payload,
            actor_user_id=actor.id,
            request_id=request_id_context.get(),
        )
        return _action_response(record, changed)

    return _execute_write(db, operation)


@router.post(
    "/{grant_id}/revoke",
    response_model=AppServiceGrantActionResponse,
    summary="Revoke a service grant",
)
def revoke_service_grant(
    grant_id: int,
    payload: AppServiceGrantRevokeRequest,
    _request: Request,
    actor: User = _GRANT_REVOKE_DEPENDENCY,
    db: DbSession = _DB_DEPENDENCY,
) -> AppServiceGrantActionResponse:
    def operation() -> AppServiceGrantActionResponse:
        record, changed = AppServiceGrantService(db).revoke_grant(
            grant_id,
            actor_user_id=actor.id,
            reason=payload.reason,
            request_id=request_id_context.get(),
        )
        return _action_response(record, changed)

    return _execute_write(db, operation)
