from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.core.database import get_db
from app.deps.service_auth import ServicePrincipal, require_service_scope
from app.models.app import App
from app.schemas.admin_app import AdminAppResponse

router = APIRouter(prefix="/internal/v1", tags=["internal"])
_APPLICATION_READ_DEPENDENCY = Depends(require_service_scope("main:application:read"))
_DB_DEPENDENCY = Depends(get_db)


@router.get(
    "/applications/{app_id}",
    response_model=AdminAppResponse,
    summary="Return safe application metadata",
    responses={
        401: {"description": "Invalid service token"},
        403: {"description": "Insufficient service scope"},
        404: {"description": "Application not found"},
    },
)
def get_internal_application(
    app_id: str,
    _principal: ServicePrincipal = _APPLICATION_READ_DEPENDENCY,
    db: DbSession = _DB_DEPENDENCY,
) -> AdminAppResponse:
    application = db.scalar(select(App).where(App.app_id == app_id))
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="APP_NOT_FOUND")
    return AdminAppResponse.model_validate(application)
