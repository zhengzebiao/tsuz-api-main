from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session as DbSession

from app.api.dependencies import require_permissions
from app.core.database import get_db
from app.core.logging import request_id_context
from app.models.role import Role
from app.models.user import User
from app.schemas.admin_role import AdminRoleSummary
from app.schemas.admin_user import (
    AdminForceLogoutRequest,
    AdminForceLogoutResponse,
    AdminPasswordReset,
    AdminPasswordResetResponse,
    AdminUserActionResponse,
    AdminUserCreate,
    AdminUserListResponse,
    AdminUserResponse,
    AdminUserRoleAssignment,
    AdminUserRolesResponse,
    AdminUserUpdate,
    UserStatusReason,
)
from app.services.admin_role_service import RoleDisabledError, RoleNotFoundError
from app.services.admin_user_service import (
    AdminUserError,
    AdminUserService,
    EmailAlreadyExistsError,
    InvalidPasswordError,
    LastActiveAdminError,
    SelfOperationNotAllowedError,
    UserBlacklistedError,
    UserNotFoundError,
    UserVersionConflictError,
)

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


_ERROR_STATUS_CODES: dict[type[AdminUserError], int] = {
    InvalidPasswordError: status.HTTP_400_BAD_REQUEST,
    UserNotFoundError: status.HTTP_404_NOT_FOUND,
    EmailAlreadyExistsError: status.HTTP_409_CONFLICT,
    UserVersionConflictError: status.HTTP_409_CONFLICT,
    UserBlacklistedError: status.HTTP_409_CONFLICT,
    SelfOperationNotAllowedError: status.HTTP_409_CONFLICT,
    LastActiveAdminError: status.HTTP_409_CONFLICT,
    RoleNotFoundError: status.HTTP_404_NOT_FOUND,
    RoleDisabledError: status.HTTP_409_CONFLICT,
}


def _raise_admin_error(exc: AdminUserError | RoleNotFoundError | RoleDisabledError) -> None:
    status_code = next(
        (code for error_type, code in _ERROR_STATUS_CODES.items() if isinstance(exc, error_type)),
        status.HTTP_400_BAD_REQUEST,
    )
    raise HTTPException(status_code=status_code, detail=getattr(exc, "code", str(exc))) from exc


def _request_id() -> str:
    return request_id_context.get()


def _user_response(user: User) -> AdminUserResponse:
    return AdminUserResponse.model_validate(user)


def _action_response(user: User, changed: bool, revoked_sessions: int) -> AdminUserActionResponse:
    return AdminUserActionResponse(
        **_user_response(user).model_dump(),
        changed=changed,
        revoked_sessions=revoked_sessions,
    )


def _user_roles_response(
    user: User,
    roles: list[Role],
    changed: bool,
    revoked_sessions: int,
) -> AdminUserRolesResponse:
    return AdminUserRolesResponse(
        user_id=user.id,
        roles=[AdminRoleSummary.model_validate(role) for role in roles],
        version=user.version,
        changed=changed,
        revoked_sessions=revoked_sessions,
    )


def _execute_roles_write(db: DbSession, operation: Callable[[], AdminUserRolesResponse]) -> AdminUserRolesResponse:
    try:
        response = operation()
        db.commit()
        return response
    except (AdminUserError, RoleNotFoundError, RoleDisabledError) as exc:
        db.rollback()
        _raise_admin_error(exc)
    except Exception:
        db.rollback()
        raise
    raise AssertionError("unreachable")


@router.get("", response_model=AdminUserListResponse, summary="List users")
def list_users(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = Query(default=None, max_length=320),
    is_active: bool | None = Query(default=None),
    is_blacklisted: bool | None = Query(default=None),
    _actor: User = Depends(require_permissions("user:read")),
    db: DbSession = Depends(get_db),
) -> AdminUserListResponse:
    users, total = AdminUserService(db).list_users(
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


@router.get("/{user_id}/roles", response_model=AdminUserRolesResponse, summary="Get user roles")
def get_user_roles(
    user_id: int,
    _actor: User = Depends(require_permissions("user:read")),
    db: DbSession = Depends(get_db),
) -> AdminUserRolesResponse:
    try:
        service = AdminUserService(db)
        user = service.get_user(user_id)
        return _user_roles_response(user, service.get_user_roles(user_id), False, 0)
    except (AdminUserError, RoleNotFoundError, RoleDisabledError) as exc:
        _raise_admin_error(exc)
    raise AssertionError("unreachable")


@router.put("/{user_id}/roles", response_model=AdminUserRolesResponse, summary="Replace user roles")
def assign_user_roles(
    user_id: int,
    payload: AdminUserRoleAssignment,
    _request: Request,
    actor: User = Depends(require_permissions("user:assign_roles")),
    db: DbSession = Depends(get_db),
) -> AdminUserRolesResponse:
    def operation() -> AdminUserRolesResponse:
        user, roles, changed, revoked_sessions = AdminUserService(db).assign_roles(
            user_id,
            payload.role_ids,
            payload.version,
            actor_user_id=actor.id,
            request_id=_request_id(),
        )
        return _user_roles_response(user, roles, changed, revoked_sessions)

    return _execute_roles_write(db, operation)


@router.get("/{user_id}", response_model=AdminUserResponse, summary="Get user details")
def get_user(
    user_id: int,
    _actor: User = Depends(require_permissions("user:read")),
    db: DbSession = Depends(get_db),
) -> AdminUserResponse:
    try:
        return _user_response(AdminUserService(db).get_user(user_id))
    except AdminUserError as exc:
        _raise_admin_error(exc)
    raise AssertionError("unreachable")


@router.post("", response_model=AdminUserResponse, status_code=status.HTTP_201_CREATED, summary="Create a user")
def create_user(
    payload: AdminUserCreate,
    _request: Request,
    actor: User = Depends(require_permissions("user:create")),
    db: DbSession = Depends(get_db),
) -> AdminUserResponse:
    try:
        user = AdminUserService(db).create_user(
            payload,
            actor_user_id=actor.id,
            request_id=_request_id(),
        )
        return _user_response(user)
    except AdminUserError as exc:
        _raise_admin_error(exc)
    raise AssertionError("unreachable")


@router.patch("/{user_id}", response_model=AdminUserActionResponse, summary="Update user profile")
def update_user(
    user_id: int,
    payload: AdminUserUpdate,
    _request: Request,
    actor: User = Depends(require_permissions("user:update")),
    db: DbSession = Depends(get_db),
) -> AdminUserActionResponse:
    try:
        user, changed, revoked_sessions = AdminUserService(db).update_user(
            user_id,
            payload,
            actor_user_id=actor.id,
            request_id=_request_id(),
        )
        return _action_response(user, changed, revoked_sessions)
    except AdminUserError as exc:
        _raise_admin_error(exc)
    raise AssertionError("unreachable")


def _status_endpoint(
    operation: Callable[..., tuple[User, bool, int]],
    user_id: int,
    actor: User,
    db: DbSession,
    reason: str | None = None,
) -> AdminUserActionResponse:
    try:
        user, changed, revoked_sessions = operation(
            user_id,
            actor_user_id=actor.id,
            reason=reason,
            request_id=_request_id(),
        )
        return _action_response(user, changed, revoked_sessions)
    except AdminUserError as exc:
        _raise_admin_error(exc)
    raise AssertionError("unreachable")


@router.post("/{user_id}/disable", response_model=AdminUserActionResponse, summary="Disable a user")
def disable_user(
    user_id: int,
    payload: UserStatusReason,
    _request: Request,
    actor: User = Depends(require_permissions("user:disable")),
    db: DbSession = Depends(get_db),
) -> AdminUserActionResponse:
    return _status_endpoint(
        lambda target_id, **kwargs: AdminUserService(db).disable_user(target_id, **kwargs),
        user_id,
        actor,
        db,
        payload.reason,
    )


@router.post("/{user_id}/enable", response_model=AdminUserActionResponse, summary="Enable a user")
def enable_user(
    user_id: int,
    _request: Request,
    actor: User = Depends(require_permissions("user:enable")),
    db: DbSession = Depends(get_db),
) -> AdminUserActionResponse:
    try:
        user, changed, revoked_sessions = AdminUserService(db).enable_user(
            user_id,
            actor_user_id=actor.id,
            request_id=_request_id(),
        )
        return _action_response(user, changed, revoked_sessions)
    except AdminUserError as exc:
        _raise_admin_error(exc)
    raise AssertionError("unreachable")


@router.post("/{user_id}/blacklist", response_model=AdminUserActionResponse, summary="Blacklist a user")
def blacklist_user(
    user_id: int,
    payload: UserStatusReason,
    _request: Request,
    actor: User = Depends(require_permissions("user:blacklist")),
    db: DbSession = Depends(get_db),
) -> AdminUserActionResponse:
    return _status_endpoint(
        lambda target_id, **kwargs: AdminUserService(db).blacklist_user(target_id, **kwargs),
        user_id,
        actor,
        db,
        payload.reason,
    )


@router.post("/{user_id}/recover", response_model=AdminUserActionResponse, summary="Recover a blacklisted user")
def recover_user(
    user_id: int,
    _request: Request,
    actor: User = Depends(require_permissions("user:recover")),
    db: DbSession = Depends(get_db),
) -> AdminUserActionResponse:
    try:
        user, changed, revoked_sessions = AdminUserService(db).recover_user(
            user_id,
            actor_user_id=actor.id,
            request_id=_request_id(),
        )
        return _action_response(user, changed, revoked_sessions)
    except AdminUserError as exc:
        _raise_admin_error(exc)
    raise AssertionError("unreachable")


@router.post("/{user_id}/reset-password", response_model=AdminPasswordResetResponse, summary="Reset a user password")
def reset_password(
    user_id: int,
    payload: AdminPasswordReset,
    _request: Request,
    actor: User = Depends(require_permissions("user:reset_password")),
    db: DbSession = Depends(get_db),
) -> AdminPasswordResetResponse:
    try:
        revoked_sessions = AdminUserService(db).reset_password(
            user_id,
            payload.new_password,
            actor_user_id=actor.id,
            request_id=_request_id(),
        )
        return AdminPasswordResetResponse(message="password reset", revoked_sessions=revoked_sessions)
    except AdminUserError as exc:
        _raise_admin_error(exc)
    raise AssertionError("unreachable")


@router.post("/{user_id}/force-logout", response_model=AdminForceLogoutResponse, summary="Force all user sessions to log out")
def force_logout(
    user_id: int,
    payload: AdminForceLogoutRequest | None = None,
    _request: Request = None,
    actor: User = Depends(require_permissions("user:force_logout")),
    db: DbSession = Depends(get_db),
) -> AdminForceLogoutResponse:
    try:
        revoked_sessions = AdminUserService(db).force_logout(
            user_id,
            actor_user_id=actor.id,
            reason=payload.reason if payload else None,
            request_id=_request_id(),
        )
        return AdminForceLogoutResponse(message="user logged out", revoked_sessions=revoked_sessions)
    except AdminUserError as exc:
        _raise_admin_error(exc)
    raise AssertionError("unreachable")
