from collections.abc import Callable
from typing import TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session as DbSession

from app.api.dependencies import require_permissions
from app.core.database import get_db
from app.core.logging import request_id_context
from app.models.user import User
from app.schemas.admin_permission import (
    AdminPermissionActionResponse,
    AdminPermissionDetailResponse,
    AdminPermissionDisableRequest,
    AdminPermissionEndpointResponse,
    AdminPermissionListResponse,
    AdminPermissionResponse,
    AdminPermissionUpdate,
)
from app.services.admin_permission_service import (
    AdminPermissionError,
    AdminPermissionService,
    PermissionNotDeclaredError,
    PermissionNotFoundError,
    PermissionRecord,
    PermissionVersionConflictError,
    ProtectedPermissionOperationError,
)

router = APIRouter(prefix="/admin/permissions", tags=["admin-permissions"])


_ERROR_STATUS_CODES: dict[type[AdminPermissionError], int] = {
    PermissionNotFoundError: status.HTTP_404_NOT_FOUND,
    PermissionVersionConflictError: status.HTTP_409_CONFLICT,
    ProtectedPermissionOperationError: status.HTTP_409_CONFLICT,
    PermissionNotDeclaredError: status.HTTP_409_CONFLICT,
}

_ResponseModel = TypeVar("_ResponseModel", bound=BaseModel)


def _raise_admin_error(exc: AdminPermissionError) -> None:
    status_code = next(
        (
            code
            for error_type, code in _ERROR_STATUS_CODES.items()
            if isinstance(exc, error_type)
        ),
        status.HTTP_400_BAD_REQUEST,
    )
    raise HTTPException(
        status_code=status_code,
        detail=getattr(exc, "code", AdminPermissionError.code),
    ) from exc


def _request_id() -> str:
    return request_id_context.get()


def _permission_response(record: PermissionRecord) -> AdminPermissionResponse:
    permission = record.permission
    resource, action = permission.name.split(":", 1)
    return AdminPermissionResponse(
        id=permission.id,
        name=permission.name,
        display_name=permission.display_name,
        description=permission.description,
        resource=resource,
        action=action,
        is_declared=permission.is_declared,
        is_enabled=permission.is_enabled,
        disabled_at=permission.disabled_at,
        disabled_reason=permission.disabled_reason,
        missing_at=permission.missing_at,
        endpoint_count=record.endpoint_count,
        role_count=record.role_count,
        created_at=permission.created_at,
        updated_at=permission.updated_at,
        version=permission.version,
    )


def _detail_response(record: PermissionRecord) -> AdminPermissionDetailResponse:
    return AdminPermissionDetailResponse(
        **_permission_response(record).model_dump(),
        endpoints=[
            AdminPermissionEndpointResponse.model_validate(endpoint, from_attributes=True)
            for endpoint in record.endpoints
        ],
    )


def _action_response(
    record: PermissionRecord,
    changed: bool,
    revoked_sessions: int,
) -> AdminPermissionActionResponse:
    return AdminPermissionActionResponse(
        **_permission_response(record).model_dump(),
        changed=changed,
        revoked_sessions=revoked_sessions,
    )


def _execute_write(
    db: DbSession,
    operation: Callable[[], _ResponseModel],
) -> _ResponseModel:
    try:
        response = operation()
        db.commit()
        return response
    except AdminPermissionError as exc:
        db.rollback()
        _raise_admin_error(exc)
    except Exception:
        db.rollback()
        raise
    raise AssertionError("unreachable")


@router.get("", response_model=AdminPermissionListResponse, summary="List permissions")
def list_permissions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = Query(default=None, max_length=255),
    resource: str | None = Query(
        default=None,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    ),
    is_declared: bool | None = Query(default=None),
    is_enabled: bool | None = Query(default=None),
    _actor: User = Depends(require_permissions("permission:read")),
    db: DbSession = Depends(get_db),
) -> AdminPermissionListResponse:
    permissions, total = AdminPermissionService(db).list_permissions(
        page=page,
        page_size=page_size,
        keyword=keyword,
        resource=resource,
        is_declared=is_declared,
        is_enabled=is_enabled,
    )
    return AdminPermissionListResponse(
        items=[_permission_response(record) for record in permissions],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{permission_id}",
    response_model=AdminPermissionDetailResponse,
    summary="Get permission details",
)
def get_permission(
    permission_id: int,
    _actor: User = Depends(require_permissions("permission:read")),
    db: DbSession = Depends(get_db),
) -> AdminPermissionDetailResponse:
    try:
        return _detail_response(AdminPermissionService(db).get_permission(permission_id))
    except AdminPermissionError as exc:
        _raise_admin_error(exc)
    raise AssertionError("unreachable")


@router.patch(
    "/{permission_id}",
    response_model=AdminPermissionActionResponse,
    summary="Update permission display metadata",
)
def update_permission(
    permission_id: int,
    payload: AdminPermissionUpdate,
    _request: Request,
    actor: User = Depends(require_permissions("permission:update")),
    db: DbSession = Depends(get_db),
) -> AdminPermissionActionResponse:
    def operation() -> AdminPermissionActionResponse:
        record, changed, revoked_sessions = AdminPermissionService(db).update_permission(
            permission_id,
            payload,
            actor_user_id=actor.id,
            request_id=_request_id(),
        )
        return _action_response(record, changed, revoked_sessions)

    return _execute_write(db, operation)


@router.post(
    "/{permission_id}/disable",
    response_model=AdminPermissionActionResponse,
    summary="Disable permission",
)
def disable_permission(
    permission_id: int,
    payload: AdminPermissionDisableRequest,
    _request: Request,
    actor: User = Depends(require_permissions("permission:disable")),
    db: DbSession = Depends(get_db),
) -> AdminPermissionActionResponse:
    def operation() -> AdminPermissionActionResponse:
        record, changed, revoked_sessions = AdminPermissionService(db).disable_permission(
            permission_id,
            actor_user_id=actor.id,
            reason=payload.reason,
            request_id=_request_id(),
        )
        return _action_response(record, changed, revoked_sessions)

    return _execute_write(db, operation)


@router.post(
    "/{permission_id}/enable",
    response_model=AdminPermissionActionResponse,
    summary="Enable permission",
)
def enable_permission(
    permission_id: int,
    _request: Request,
    actor: User = Depends(require_permissions("permission:enable")),
    db: DbSession = Depends(get_db),
) -> AdminPermissionActionResponse:
    def operation() -> AdminPermissionActionResponse:
        record, changed, revoked_sessions = AdminPermissionService(db).enable_permission(
            permission_id,
            actor_user_id=actor.id,
            request_id=_request_id(),
        )
        return _action_response(record, changed, revoked_sessions)

    return _execute_write(db, operation)
