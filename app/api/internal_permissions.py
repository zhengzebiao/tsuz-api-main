from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DbSession

from app.core.database import get_db
from app.deps.service_auth import ServicePrincipal, require_service_scope
from app.schemas.internal_permission_report import PermissionReportRequest, PermissionReportResponse
from app.services.permission_report_service import (
    PermissionReportAdminRoleRequiredError,
    PermissionReportCallerDisabledError,
    PermissionReportCallerNotFoundError,
    PermissionReportError,
    PermissionReportNameConflictError,
    PermissionReportService,
)

router = APIRouter(prefix="/internal/v1/permissions", tags=["internal-permissions"])
_REPORT_DEPENDENCY = Depends(require_service_scope("main:permission:report"))
_DB_DEPENDENCY = Depends(get_db)


@router.put(
    "/report",
    response_model=PermissionReportResponse,
    summary="Report a sub-application permission snapshot",
    responses={
        401: {"description": "Invalid service token"},
        403: {"description": "Insufficient service scope"},
        409: {"description": "Permission name conflict"},
        503: {"description": "Permission reporting unavailable"},
    },
)
def report_permissions(
    payload: PermissionReportRequest,
    request: Request,
    principal: ServicePrincipal = _REPORT_DEPENDENCY,
    db: DbSession = _DB_DEPENDENCY,
) -> PermissionReportResponse:
    try:
        summary = PermissionReportService(db).report(
            principal.caller_app_id,
            payload.permissions,
            request_id=request.headers.get("X-Request-ID"),
        )
        db.commit()
    except PermissionReportNameConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.code) from exc
    except (
        PermissionReportCallerNotFoundError,
        PermissionReportCallerDisabledError,
        PermissionReportAdminRoleRequiredError,
    ) as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PERMISSION_REPORT_UNAVAILABLE",
        ) from exc
    except PermissionReportError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PERMISSION_REPORT_UNAVAILABLE",
        ) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PERMISSION_REPORT_UNAVAILABLE",
        ) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PERMISSION_REPORT_UNAVAILABLE",
        ) from exc

    return PermissionReportResponse(
        caller_app_id=summary.caller_app_id,
        created=summary.created,
        restored=summary.restored,
        marked_missing=summary.marked_missing,
        admin_grants_added=summary.admin_grants_added,
        sessions_revoked=summary.sessions_revoked,
        unchanged=summary.unchanged,
    )
