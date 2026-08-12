from collections.abc import Callable
from typing import TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session as DbSession

from app.api.dependencies import require_permissions
from app.core.database import get_db
from app.core.logging import request_id_context
from app.models.app import App
from app.models.user import User
from app.schemas.admin_app import (
    AdminAppActionResponse,
    AdminAppCreate,
    AdminAppCreateResponse,
    AdminAppDisableRequest,
    AdminAppListResponse,
    AdminAppRegenerateSecretRequest,
    AdminAppResponse,
    AdminAppSecretResponse,
    AdminAppUpdate,
)
from app.services.admin_app_service import (
    AdminAppError,
    AdminAppService,
    AppCreationError,
    AppNotFoundError,
    AppSecretGenerationError,
    AppVersionConflictError,
)

router = APIRouter(prefix="/admin/apps", tags=["admin-apps"])


_ERROR_STATUS_CODES: dict[type[AdminAppError], int] = {
    AppNotFoundError: status.HTTP_404_NOT_FOUND,
    AppVersionConflictError: status.HTTP_409_CONFLICT,
    AppCreationError: status.HTTP_500_INTERNAL_SERVER_ERROR,
    AppSecretGenerationError: status.HTTP_500_INTERNAL_SERVER_ERROR,
}

_ResponseModel = TypeVar("_ResponseModel", bound=BaseModel)


def _raise_admin_error(exc: AdminAppError) -> None:
    status_code = next(
        (code for error_type, code in _ERROR_STATUS_CODES.items() if isinstance(exc, error_type)),
        status.HTTP_400_BAD_REQUEST,
    )
    raise HTTPException(status_code=status_code, detail=getattr(exc, "code", AdminAppError.code)) from exc


def _request_id() -> str:
    return request_id_context.get()


def _app_response(app: App) -> AdminAppResponse:
    return AdminAppResponse.model_validate(app)


def _action_response(app: App, changed: bool) -> AdminAppActionResponse:
    return AdminAppActionResponse(**_app_response(app).model_dump(), changed=changed)


def _execute_write(db: DbSession, operation: Callable[[], _ResponseModel]) -> _ResponseModel:
    try:
        response = operation()
        db.commit()
        return response
    except AdminAppError as exc:
        db.rollback()
        _raise_admin_error(exc)
    except Exception:
        db.rollback()
        raise
    raise AssertionError("unreachable")


@router.get("", response_model=AdminAppListResponse, summary="List apps")
def list_apps(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = Query(default=None, max_length=128),
    is_enabled: bool | None = Query(default=None),
    _actor: User = Depends(require_permissions("app:read")),
    db: DbSession = Depends(get_db),
) -> AdminAppListResponse:
    apps, total = AdminAppService(db).list_apps(
        page=page,
        page_size=page_size,
        keyword=keyword,
        is_enabled=is_enabled,
    )
    return AdminAppListResponse(
        items=[_app_response(app) for app in apps],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{app_id}", response_model=AdminAppResponse, summary="Get app details")
def get_app(
    app_id: int,
    _actor: User = Depends(require_permissions("app:read")),
    db: DbSession = Depends(get_db),
) -> AdminAppResponse:
    try:
        return _app_response(AdminAppService(db).get_app(app_id))
    except AdminAppError as exc:
        _raise_admin_error(exc)
    raise AssertionError("unreachable")


@router.post(
    "",
    response_model=AdminAppCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an app",
)
def create_app(
    payload: AdminAppCreate,
    _request: Request,
    response: Response,
    actor: User = Depends(require_permissions("app:create")),
    db: DbSession = Depends(get_db),
) -> AdminAppCreateResponse:
    def operation() -> AdminAppCreateResponse:
        app, app_secret = AdminAppService(db).create_app(
            payload,
            actor_user_id=actor.id,
            request_id=_request_id(),
        )
        return AdminAppCreateResponse(app=_app_response(app), app_secret=app_secret)

    result = _execute_write(db, operation)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.patch("/{app_id}", response_model=AdminAppActionResponse, summary="Update app profile")
def update_app(
    app_id: int,
    payload: AdminAppUpdate,
    _request: Request,
    actor: User = Depends(require_permissions("app:update")),
    db: DbSession = Depends(get_db),
) -> AdminAppActionResponse:
    def operation() -> AdminAppActionResponse:
        app, changed = AdminAppService(db).update_app(
            app_id,
            payload,
            actor_user_id=actor.id,
            request_id=_request_id(),
        )
        return _action_response(app, changed)

    return _execute_write(db, operation)


@router.post("/{app_id}/disable", response_model=AdminAppActionResponse, summary="Disable an app")
def disable_app(
    app_id: int,
    payload: AdminAppDisableRequest,
    _request: Request,
    actor: User = Depends(require_permissions("app:disable")),
    db: DbSession = Depends(get_db),
) -> AdminAppActionResponse:
    def operation() -> AdminAppActionResponse:
        app, changed = AdminAppService(db).disable_app(
            app_id,
            actor_user_id=actor.id,
            reason=payload.reason,
            request_id=_request_id(),
        )
        return _action_response(app, changed)

    return _execute_write(db, operation)


@router.post("/{app_id}/enable", response_model=AdminAppActionResponse, summary="Enable an app")
def enable_app(
    app_id: int,
    _request: Request,
    actor: User = Depends(require_permissions("app:enable")),
    db: DbSession = Depends(get_db),
) -> AdminAppActionResponse:
    def operation() -> AdminAppActionResponse:
        app, changed = AdminAppService(db).enable_app(
            app_id,
            actor_user_id=actor.id,
            request_id=_request_id(),
        )
        return _action_response(app, changed)

    return _execute_write(db, operation)


@router.post(
    "/{app_id}/regenerate-secret",
    response_model=AdminAppSecretResponse,
    summary="Regenerate an app secret",
)
def regenerate_secret(
    app_id: int,
    payload: AdminAppRegenerateSecretRequest,
    _request: Request,
    response: Response,
    actor: User = Depends(require_permissions("app:regenerate_secret")),
    db: DbSession = Depends(get_db),
) -> AdminAppSecretResponse:
    def operation() -> AdminAppSecretResponse:
        app, app_secret = AdminAppService(db).regenerate_secret(
            app_id,
            actor_user_id=actor.id,
            reason=payload.reason,
            request_id=_request_id(),
        )
        return AdminAppSecretResponse(
            app_id=app.app_id,
            app_secret=app_secret,
            secret_updated_at=app.secret_updated_at,
        )

    result = _execute_write(db, operation)
    response.headers["Cache-Control"] = "no-store"
    return result
