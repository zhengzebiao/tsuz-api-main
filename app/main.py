from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.admin_apps import router as admin_apps_router
from app.api.admin_permissions import router as admin_permissions_router
from app.api.admin_roles import router as admin_roles_router
from app.api.admin_users import router as admin_users_router
from app.api.auth import router as feature_router
from app.api.health import router as health_router
from app.core.config import settings
from app.core.logging import RequestIdMiddleware, configure_logging


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title=settings.service_name,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.redoc_enabled else None,
        openapi_url="/openapi.json" if settings.openapi_enabled else None,
    )
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health_router)
    app.include_router(feature_router)
    app.include_router(admin_users_router)
    app.include_router(admin_apps_router)
    app.include_router(admin_roles_router)
    app.include_router(admin_permissions_router)
    return app


app = create_app()
