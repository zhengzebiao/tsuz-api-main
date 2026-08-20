from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.core.logging import request_id_context
from app.core.security import (
    generate_app_id,
    generate_app_secret,
    hash_app_secret,
    verify_app_secret,
)
from app.models.app import App
from app.models.audit_event import AuditEvent
from app.schemas.admin_app import AdminAppCreate, AdminAppUpdate


class AdminAppError(ValueError):
    code = "ADMIN_APP_ERROR"


class AppNotFoundError(AdminAppError):
    code = "APP_NOT_FOUND"


class AppVersionConflictError(AdminAppError):
    code = "APP_VERSION_CONFLICT"


class AppCreationError(AdminAppError):
    code = "APP_CREATION_FAILED"


class AppSecretGenerationError(AdminAppError):
    code = "APP_SECRET_GENERATION_FAILED"


class AdminAppService:
    APP_ID_GENERATION_ATTEMPTS = 5
    APP_SECRET_GENERATION_ATTEMPTS = 5

    def __init__(self, db: DbSession) -> None:
        self.db = db

    def list_apps(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        keyword: str | None = None,
        is_enabled: bool | None = None,
    ) -> tuple[list[App], int]:
        query = select(App)
        count_query = select(func.count()).select_from(App)
        filters: list[Any] = []
        if keyword:
            normalized_keyword = keyword.strip().lower()
            if normalized_keyword:
                filters.append(
                    or_(
                        func.lower(App.name).contains(normalized_keyword),
                        func.lower(App.app_id).contains(normalized_keyword),
                    )
                )
        if is_enabled is not None:
            filters.append(App.is_enabled == is_enabled)
        if filters:
            query = query.where(*filters)
            count_query = count_query.where(*filters)
        total = self.db.scalar(count_query) or 0
        apps = self.db.scalars(
            query.order_by(App.created_at.desc(), App.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return list(apps), total

    def get_app(self, app_id: int) -> App:
        app = self.db.get(App, app_id)
        if app is None:
            raise AppNotFoundError(AppNotFoundError.code)
        return app

    def create_app(
        self,
        payload: AdminAppCreate,
        *,
        actor_user_id: int,
        request_id: str | None = None,
    ) -> tuple[App, str]:
        app_secret = generate_app_secret()
        app_secret_hash = hash_app_secret(app_secret)
        app: App | None = None

        for _attempt in range(self.APP_ID_GENERATION_ATTEMPTS):
            candidate_app_id = generate_app_id()
            candidate = App(
                app_id=candidate_app_id,
                app_secret_hash=app_secret_hash,
                name=payload.name,
                icon_url=payload.icon_url,
                access_url=payload.access_url,
                service_account_name=payload.service_account_name,
            )
            try:
                with self.db.begin_nested():
                    self.db.add(candidate)
                    self.db.flush()
            except IntegrityError:
                if self.db.scalar(select(App.id).where(App.app_id == candidate_app_id)) is not None:
                    continue
                raise AppCreationError(AppCreationError.code) from None
            app = candidate
            break

        if app is None:
            raise AppCreationError(AppCreationError.code)

        self._add_audit(
            actor_user_id=actor_user_id,
            action="app.created",
            target_id=app.id,
            result="success",
            request_id=request_id,
            changes={
                "app_id": {"from": None, "to": app.app_id},
                "name": {"from": None, "to": app.name},
                "access_url": {"from": None, "to": app.access_url},
                "service_account_name": {"from": None, "to": app.service_account_name},
                "is_enabled": {"from": None, "to": app.is_enabled},
            },
        )
        self.db.flush()
        return app, app_secret

    def update_app(
        self,
        app_id: int,
        payload: AdminAppUpdate,
        *,
        actor_user_id: int,
        request_id: str | None = None,
    ) -> tuple[App, bool]:
        current = self.get_app(app_id)
        if current.version != payload.version:
            raise AppVersionConflictError(AppVersionConflictError.code)

        submitted = payload.model_dump(exclude_unset=True, exclude={"version"})
        changes = {key: value for key, value in submitted.items() if getattr(current, key) != value}
        if not changes:
            return current, False

        before = {key: getattr(current, key) for key in changes}
        result = self.db.execute(
            update(App)
            .where(App.id == app_id, App.version == payload.version)
            .values(**changes, version=App.version + 1, updated_at=self._now())
        )
        if result.rowcount != 1:
            if self.db.scalar(select(App.id).where(App.id == app_id)) is None:
                raise AppNotFoundError(AppNotFoundError.code)
            raise AppVersionConflictError(AppVersionConflictError.code)

        self.db.refresh(current)
        self._add_audit(
            actor_user_id=actor_user_id,
            action="app.updated",
            target_id=current.id,
            result="success",
            request_id=request_id,
            changes={key: {"from": before[key], "to": getattr(current, key)} for key in changes},
        )
        self.db.flush()
        return current, True

    def disable_app(
        self,
        app_id: int,
        *,
        actor_user_id: int,
        reason: str | None = None,
        request_id: str | None = None,
    ) -> tuple[App, bool]:
        app = self._lock_app(app_id)
        if not app.is_enabled:
            return app, False

        before = {
            "is_enabled": app.is_enabled,
            "disabled_at": app.disabled_at,
            "disabled_reason": app.disabled_reason,
        }
        now = self._now()
        app.is_enabled = False
        app.disabled_at = now
        app.disabled_reason = reason
        self._increment_version(app, now)
        self._add_change_audit(
            app=app,
            action="app.disabled",
            actor_user_id=actor_user_id,
            reason=reason,
            request_id=request_id,
            before=before,
        )
        self.db.flush()
        return app, True

    def enable_app(
        self,
        app_id: int,
        *,
        actor_user_id: int,
        request_id: str | None = None,
    ) -> tuple[App, bool]:
        app = self._lock_app(app_id)
        if app.is_enabled:
            return app, False

        before = {
            "is_enabled": app.is_enabled,
            "disabled_at": app.disabled_at,
            "disabled_reason": app.disabled_reason,
        }
        now = self._now()
        app.is_enabled = True
        app.disabled_at = None
        app.disabled_reason = None
        self._increment_version(app, now)
        self._add_change_audit(
            app=app,
            action="app.enabled",
            actor_user_id=actor_user_id,
            reason=None,
            request_id=request_id,
            before=before,
        )
        self.db.flush()
        return app, True

    def regenerate_secret(
        self,
        app_id: int,
        *,
        actor_user_id: int,
        reason: str,
        request_id: str | None = None,
    ) -> tuple[App, str]:
        app = self._lock_app(app_id)
        app_secret: str | None = None
        app_secret_hash: str | None = None
        for _attempt in range(self.APP_SECRET_GENERATION_ATTEMPTS):
            candidate = generate_app_secret()
            if verify_app_secret(candidate, app.app_secret_hash):
                continue
            app_secret = candidate
            app_secret_hash = hash_app_secret(candidate)
            break
        if app_secret is None or app_secret_hash is None:
            raise AppSecretGenerationError(AppSecretGenerationError.code)

        now = self._now()
        app.app_secret_hash = app_secret_hash
        app.secret_updated_at = now
        self._increment_version(app, now)
        self._add_audit(
            actor_user_id=actor_user_id,
            action="app.secret_regenerated",
            target_id=app.id,
            result="success",
            reason=reason,
            request_id=request_id,
            changes={"secret_changed": True},
        )
        self.db.flush()
        return app, app_secret

    def _lock_app(self, app_id: int) -> App:
        app = self.db.scalar(select(App).where(App.id == app_id).with_for_update())
        if app is None:
            raise AppNotFoundError(AppNotFoundError.code)
        return app

    def _add_change_audit(
        self,
        *,
        app: App,
        action: str,
        actor_user_id: int,
        reason: str | None,
        request_id: str | None,
        before: dict[str, Any],
    ) -> None:
        self._add_audit(
            actor_user_id=actor_user_id,
            action=action,
            target_id=app.id,
            result="success",
            reason=reason,
            request_id=request_id,
            changes={key: {"from": value, "to": getattr(app, key)} for key, value in before.items()},
        )

    def _add_audit(
        self,
        *,
        actor_user_id: int,
        action: str,
        target_id: int,
        result: str,
        request_id: str | None,
        reason: str | None = None,
        changes: dict[str, Any] | None = None,
    ) -> None:
        self.db.add(
            AuditEvent(
                actor_user_id=actor_user_id,
                action=action,
                target_type="app",
                target_id=target_id,
                result=result,
                reason=reason,
                changes_json=self._json_safe(changes),
                request_id=request_id or request_id_context.get() or "unknown",
            )
        )

    def _json_safe(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {key: self._json_safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._json_safe(item) for item in value]
        return value

    def _increment_version(self, app: App, now: datetime) -> None:
        app.version += 1
        app.updated_at = now

    def _now(self) -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)
