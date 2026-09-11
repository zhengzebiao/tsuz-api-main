from collections.abc import Callable
from typing import TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session as DbSession

from app.api.dependencies import require_permissions
from app.core.database import get_db
from app.core.logging import request_id_context
from app.models.resource_scope import ResourceScope
from app.models.user import User
from app.schemas.service_authorization import (
    ResourceScopeActionResponse,
    ResourceScopeCreate,
    ResourceScopeListResponse,
    ResourceScopeResponse,
)
from app.services.resource_scope_service import (
    ResourceScopeAlreadyExistsError,
    ResourceScopeError,
    ResourceScopeNotFoundError,
    ResourceScopeService,
    ResourceScopeTargetNotFoundError,
)

router = APIRouter(prefix="/admin/resource-scopes", tags=["admin-resource-scopes"])

_SCOPE_READ_DEPENDENCY = Depends(require_permissions("resource_scope:read"))
_SCOPE_CREATE_DEPENDENCY = Depends(require_permissions("resource_scope:create"))
_SCOPE_DISABLE_DEPENDENCY = Depends(require_permissions("resource_scope:disable"))
_SCOPE_ENABLE_DEPENDENCY = Depends(require_permissions("resource_scope:enable"))
_DB_DEPENDENCY = Depends(get_db)

_ERROR_STATUS_CODES: dict[type[ResourceScopeError], int] = {
    ResourceScopeNotFoundError: status.HTTP_404_NOT_FOUND,
    ResourceScopeTargetNotFoundError: status.HTTP_404_NOT_FOUND,
    ResourceScopeAlreadyExistsError: status.HTTP_409_CONFLICT,
}
_ResponseModel = TypeVar("_ResponseModel", bound=BaseModel)


def _scope_response(scope: ResourceScope) -> ResourceScopeResponse:
    return ResourceScopeResponse.model_validate(scope)


def _action_response(scope: ResourceScope, changed: bool) -> ResourceScopeActionResponse:
    return ResourceScopeActionResponse(**_scope_response(scope).model_dump(), changed=changed)


def _raise_admin_error(exc: ResourceScopeError) -> None:
    status_code = next(
        (code for error_type, code in _ERROR_STATUS_CODES.items() if isinstance(exc, error_type)),
        status.HTTP_400_BAD_REQUEST,
    )
    raise HTTPException(status_code=status_code, detail=getattr(exc, "code", ResourceScopeError.code)) from exc


def _execute_write(db: DbSession, operation: Callable[[], _ResponseModel]) -> _ResponseModel:
    try:
        response = operation()
        db.commit()
        return response
    except ResourceScopeError as exc:
        db.rollback()
        _raise_admin_error(exc)
    except Exception:
        db.rollback()
        raise
    raise AssertionError("unreachable")


@router.get("", response_model=ResourceScopeListResponse, summary="List resource scopes")
def list_resource_scopes(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    target_app_id: str | None = Query(default=None, max_length=64),
    is_enabled: bool | None = Query(default=None),
    _actor: User = _SCOPE_READ_DEPENDENCY,
    db: DbSession = _DB_DEPENDENCY,
) -> ResourceScopeListResponse:
    scopes, total = ResourceScopeService(db).list_scopes(
        page=page,
        page_size=page_size,
        target_app_id=target_app_id,
        is_enabled=is_enabled,
    )
    return ResourceScopeListResponse(
        items=[_scope_response(scope) for scope in scopes],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "",
    response_model=ResourceScopeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a resource scope",
)
def create_resource_scope(
    payload: ResourceScopeCreate,
    _request: Request,
    actor: User = _SCOPE_CREATE_DEPENDENCY,
    db: DbSession = _DB_DEPENDENCY,
) -> ResourceScopeResponse:
    return _execute_write(
        db,
        lambda: _scope_response(
            ResourceScopeService(db).create_scope(
                payload,
                actor_user_id=actor.id,
                request_id=request_id_context.get(),
            )
        ),
    )


@router.post(
    "/{scope_id}/disable",
    response_model=ResourceScopeActionResponse,
    summary="Disable a resource scope",
)
def disable_resource_scope(
    scope_id: int,
    _request: Request,
    actor: User = _SCOPE_DISABLE_DEPENDENCY,
    db: DbSession = _DB_DEPENDENCY,
) -> ResourceScopeActionResponse:
    def operation() -> ResourceScopeActionResponse:
        scope, changed = ResourceScopeService(db).disable_scope(
            scope_id,
            actor_user_id=actor.id,
            request_id=request_id_context.get(),
        )
        return _action_response(scope, changed)

    return _execute_write(db, operation)


@router.post(
    "/{scope_id}/enable",
    response_model=ResourceScopeActionResponse,
    summary="Enable a resource scope",
)
def enable_resource_scope(
    scope_id: int,
    _request: Request,
    actor: User = _SCOPE_ENABLE_DEPENDENCY,
    db: DbSession = _DB_DEPENDENCY,
) -> ResourceScopeActionResponse:
    def operation() -> ResourceScopeActionResponse:
        scope, changed = ResourceScopeService(db).enable_scope(
            scope_id,
            actor_user_id=actor.id,
            request_id=request_id_context.get(),
        )
        return _action_response(scope, changed)

    return _execute_write(db, operation)
