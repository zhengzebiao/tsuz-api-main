from fastapi import APIRouter

from app.core.config import settings

router = APIRouter(tags=["health"])


@router.get("/health", summary="Service health check")
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name, "env": settings.app_env}
