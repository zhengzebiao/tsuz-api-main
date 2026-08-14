from collections.abc import Callable
from typing import TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session as DbSession

from app.api.dependencies import require_permissions
from app.core.database import get_db
from app.core.logging import request_id_context
from app.models.permission import Permission
from app.models.role import Role
from app.models.user import User
from app.schemas.admin_role import (
    AdminRoleActionResponse,
    AdminRoleCreate,
    AdminRoleDisableRequest,
    AdminRoleListResponse,
    AdminRolePermissionAssignment,
    AdminRolePermissionSummary,
    AdminRolePermissionsResponse,
    AdminRoleResponse,
    AdminRoleUpdate,
)
from app.schemas.admin_user import AdminUserListResponse, AdminUserResponse
from app.services.admin_permission_service import (
    AdminPermissionError,
    PermissionDisabledError,
    PermissionNotDeclaredError,
    PermissionNotFoundError,
)
from app.services.admin_role_service import (
    AdminRoleError,
    AdminRoleService,
    ProtectedRoleOperationError,
    RoleNameAlreadyExistsError,
    RoleNotFoundError,
    RoleVersionConflictError,
)

router = APIRouter(prefix="/admin/roles", tags=["admin-roles"])


_ERROR_STATUS_CODES: dict[type[AdminRoleError | AdminPermissionError], int] = {
    RoleNotFoundError: status.HTTP_404_NOT_FOUND,
    PermissionNotFoundError: status.HTTP_404_NOT_FOUND,
    RoleNameAlreadyExistsError: status.HTTP_409_CONFLICT,
    RoleVersionConflictError: status.HTTP_409_CONFLICT,
    ProtectedRoleOperationError: status.HTTP_409_CONFLICT,
    PermissionNotDeclaredError: status.HTTP_409_CONFLICT,
    PermissionDisabledError: status.HTTP_409_CONFLICT,
}

_ResponseModel = TypeVar("_ResponseModel", bound=BaseModel)


def _raise_admin_error(exc: AdminRoleError | AdminPermissionError) -> None:
    status_code = next(
        (code for error_type, code in _ERROR_STATUS_CODES.items() if isinstance(exc, error_type)),
        status.HTTP_400_BAD_REQUEST,
    )
    raise HTTPException(status_code=status_code, detail=getattr(exc, "code", AdminRoleError.code)) from exc


def _request_id() -> str:
    return request_id_context.get()


def _role_response(role: Role) -> AdminRoleResponse:
    return AdminRoleResponse.model_validate(role)


def _action_response(role: Role, changed: bool, revoked_sessions: int) -> AdminRoleActionResponse:
    return AdminRoleActionResponse(
        **_role_response(role).model_dump(),
        changed=changed,
        revoked_sessions=revoked_sessions,
    )


def _role_permissions_response(
    role: Role,
    permissions: list[Permission],
    changed: bool,
    revoked_sessions: int,
) -> AdminRolePermissionsResponse:
    return AdminRolePermissionsResponse(
        role_id=role.id,
        permissions=[
            AdminRolePermissionSummary.model_validate(permission)
            for permission in permissions
        ],
        version=role.version,
        changed=changed,
        revoked_sessions=revoked_sessions,
    )


def _user_response(user: User) -> AdminUserResponse:
    return AdminUserResponse.model_validate(user)


def _execute_write(db: DbSession, operation: Callable[[], _ResponseModel]) -> _ResponseModel:
    try:
        response = operation()
        db.commit()
        return response
    except (AdminRoleError, AdminPermissionError) as exc:
        db.rollback()
        _raise_admin_error(exc)
    except Exception:
        db.rollback()
        raise
    raise AssertionError("unreachable")


@router.get("", response_model=AdminRoleListResponse, summary="List roles")
def list_roles(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = Query(default=None, max_length=255),
    is_enabled: bool | None = Query(default=None),
    _actor: User = Depends(require_permissions("role:read")),
    db: DbSession = Depends(get_db),
) -> AdminRoleListResponse:
    roles, total = AdminRoleService(db).list_roles(
        page=page,
        page_size=page_size,
        keyword=keyword,
        is_enabled=is_enabled,
    )
    return AdminRoleListResponse(
        items=[_role_response(role) for role in roles],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{role_id}", response_model=AdminRoleResponse, summary="Get role details")
def get_role(
    role_id: int,
    _actor: User = Depends(require_permissions("role:read")),
    db: DbSession = Depends(get_db),
) -> AdminRoleResponse:
    try:
        return _role_response(AdminRoleService(db).get_role(role_id))
    except AdminRoleError as exc:
        _raise_admin_error(exc)
    raise AssertionError("unreachable")


@router.post(
    "",
    response_model=AdminRoleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a role",
)
def create_role(
    payload: AdminRoleCreate,
    _request: Request,
    actor: User = Depends(require_permissions("role:create")),
    db: DbSession = Depends(get_db),
) -> AdminRoleResponse:
    def operation() -> AdminRoleResponse:
        role = AdminRoleService(db).create_role(
            payload,
            actor_user_id=actor.id,
            request_id=_request_id(),
        )
        return _role_response(role)

    return _execute_write(db, operation)


@router.patch("/{role_id}", response_model=AdminRoleActionResponse, summary="Update role")
def update_role(
    role_id: int,
    payload: AdminRoleUpdate,
    _request: Request,
    actor: User = Depends(require_permissions("role:update")),
    db: DbSession = Depends(get_db),
) -> AdminRoleActionResponse:
    def operation() -> AdminRoleActionResponse:
        role, changed, revoked_sessions = AdminRoleService(db).update_role(
            role_id,
            payload,
            actor_user_id=actor.id,
            request_id=_request_id(),
        )
        return _action_response(role, changed, revoked_sessions)

    return _execute_write(db, operation)


@router.post(
    "/{role_id}/disable",
    response_model=AdminRoleActionResponse,
    summary="Disable a role",
)
def disable_role(
    role_id: int,
    payload: AdminRoleDisableRequest,
    _request: Request,
    actor: User = Depends(require_permissions("role:disable")),
    db: DbSession = Depends(get_db),
) -> AdminRoleActionResponse:
    def operation() -> AdminRoleActionResponse:
        role, changed, revoked_sessions = AdminRoleService(db).disable_role(
            role_id,
            actor_user_id=actor.id,
            reason=payload.reason,
            request_id=_request_id(),
        )
        return _action_response(role, changed, revoked_sessions)

    return _execute_write(db, operation)


@router.post(
    "/{role_id}/enable",
    response_model=AdminRoleActionResponse,
    summary="Enable a role",
)
def enable_role(
    role_id: int,
    _request: Request,
    actor: User = Depends(require_permissions("role:enable")),
    db: DbSession = Depends(get_db),
) -> AdminRoleActionResponse:
    def operation() -> AdminRoleActionResponse:
        role, changed, revoked_sessions = AdminRoleService(db).enable_role(
            role_id,
            actor_user_id=actor.id,
            request_id=_request_id(),
        )
        return _action_response(role, changed, revoked_sessions)

    return _execute_write(db, operation)


@router.get(
    "/{role_id}/permissions",
    response_model=AdminRolePermissionsResponse,
    summary="Get role permissions",
)
def get_role_permissions(
    role_id: int,
    _actor: User = Depends(require_permissions("role:read")),
    db: DbSession = Depends(get_db),
) -> AdminRolePermissionsResponse:
    try:
        service = AdminRoleService(db)
        role = service.get_role(role_id)
        return _role_permissions_response(
            role,
            service.get_role_permissions(role_id),
            False,
            0,
        )
    except (AdminRoleError, AdminPermissionError) as exc:
        _raise_admin_error(exc)
    raise AssertionError("unreachable")


@router.put(
    "/{role_id}/permissions",
    response_model=AdminRolePermissionsResponse,
    summary="Replace role permissions",
)
def assign_role_permissions(
    role_id: int,
    payload: AdminRolePermissionAssignment,
    _request: Request,
    actor: User = Depends(require_permissions("role:assign_permissions")),
    db: DbSession = Depends(get_db),
) -> AdminRolePermissionsResponse:
    def operation() -> AdminRolePermissionsResponse:
        role, permissions, changed, revoked_sessions = (
            AdminRoleService(db).assign_permissions(
                role_id,
                payload.permission_ids,
                payload.version,
                actor_user_id=actor.id,
                request_id=_request_id(),
            )
        )
        return _role_permissions_response(
            role,
            permissions,
            changed,
            revoked_sessions,
        )

    return _execute_write(db, operation)


@router.get(
    "/{role_id}/users",
    response_model=AdminUserListResponse,
    summary="List users assigned to a role",
)
def list_role_users(
    role_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = Query(default=None, max_length=320),
    is_active: bool | None = Query(default=None),
    is_blacklisted: bool | None = Query(default=None),
    _actor: User = Depends(require_permissions("role:read")),
    db: DbSession = Depends(get_db),
) -> AdminUserListResponse:
    try:
        users, total = AdminRoleService(db).list_role_users(
            role_id,
            page=page,
            page_size=page_size,
            keyword=keyword,
            is_active=is_active,
            is_blacklisted=is_blacklisted,
        )
        return AdminUserListResponse(
            items=[_user_response(user) for user in users],
            total=total,
            page=page,
            page_size=page_size,
        )
    except AdminRoleError as exc:
        _raise_admin_error(exc)
    raise AssertionError("unreachable")
