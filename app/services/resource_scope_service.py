from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.core.logging import request_id_context
from app.models.app import App
from app.models.audit_event import AuditEvent
from app.models.resource_scope import ResourceScope
from app.schemas.service_authorization import ResourceScopeCreate


class ResourceScopeError(ValueError):
    code = "RESOURCE_SCOPE_ERROR"


class ResourceScopeNotFoundError(ResourceScopeError):
    code = "RESOURCE_SCOPE_NOT_FOUND"


class ResourceScopeTargetNotFoundError(ResourceScopeError):
    code = "RESOURCE_SCOPE_TARGET_NOT_FOUND"


class ResourceScopeAlreadyExistsError(ResourceScopeError):
    code = "RESOURCE_SCOPE_ALREADY_EXISTS"


class ResourceScopeService:
    def __init__(self, db: DbSession) -> None:
        self.db = db

    def list_scopes(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        target_app_id: str | None = None,
        is_enabled: bool | None = None,
    ) -> tuple[list[ResourceScope], int]:
        query = select(ResourceScope)
        count_query = select(func.count()).select_from(ResourceScope)
        filters: list[Any] = []
        if target_app_id:
            filters.append(ResourceScope.target_app_id == target_app_id.strip())
        if is_enabled is not None:
            filters.append(ResourceScope.is_enabled.is_(is_enabled))
        if filters:
            query = query.where(*filters)
            count_query = count_query.where(*filters)
        total = self.db.scalar(count_query) or 0
        scopes = self.db.scalars(
            query.order_by(ResourceScope.target_app_id, ResourceScope.scope_code, ResourceScope.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return list(scopes), int(total)

    def create_scope(
        self,
        payload: ResourceScopeCreate,
        *,
        actor_user_id: int,
        request_id: str | None = None,
    ) -> ResourceScope:
        if self.db.scalar(select(App.id).where(App.app_id == payload.target_app_id)) is None:
            raise ResourceScopeTargetNotFoundError(ResourceScopeTargetNotFoundError.code)
        scope = ResourceScope(
            target_app_id=payload.target_app_id,
            scope_code=payload.scope_code,
            description=payload.description,
        )
        try:
            with self.db.begin_nested():
                self.db.add(scope)
                self.db.flush()
        except IntegrityError:
            raise ResourceScopeAlreadyExistsError(ResourceScopeAlreadyExistsError.code) from None
        self._add_audit(
            scope=scope,
            action="resource_scope.created",
            actor_user_id=actor_user_id,
            request_id=request_id,
            changes={
                "target_app_id": {"from": None, "to": scope.target_app_id},
                "scope_code": {"from": None, "to": scope.scope_code},
                "is_enabled": {"from": None, "to": scope.is_enabled},
            },
        )
        self.db.flush()
        return scope

    def disable_scope(
        self,
        scope_id: int,
        *,
        actor_user_id: int,
        request_id: str | None = None,
    ) -> tuple[ResourceScope, bool]:
        scope = self._lock_scope(scope_id)
        if not scope.is_enabled:
            return scope, False
        scope.is_enabled = False
        scope.updated_at = self._now()
        self._add_audit(
            scope=scope,
            action="resource_scope.disabled",
            actor_user_id=actor_user_id,
            request_id=request_id,
            changes={"is_enabled": {"from": True, "to": False}},
        )
        self.db.flush()
        return scope, True

    def enable_scope(
        self,
        scope_id: int,
        *,
        actor_user_id: int,
        request_id: str | None = None,
    ) -> tuple[ResourceScope, bool]:
        scope = self._lock_scope(scope_id)
        if scope.is_enabled:
            return scope, False
        scope.is_enabled = True
        scope.updated_at = self._now()
        self._add_audit(
            scope=scope,
            action="resource_scope.enabled",
            actor_user_id=actor_user_id,
            request_id=request_id,
            changes={"is_enabled": {"from": False, "to": True}},
        )
        self.db.flush()
        return scope, True

    def _lock_scope(self, scope_id: int) -> ResourceScope:
        scope = self.db.scalar(
            select(ResourceScope).where(ResourceScope.id == scope_id).with_for_update()
        )
        if scope is None:
            raise ResourceScopeNotFoundError(ResourceScopeNotFoundError.code)
        return scope

    def _add_audit(
        self,
        *,
        scope: ResourceScope,
        action: str,
        actor_user_id: int,
        request_id: str | None,
        changes: dict[str, Any],
    ) -> None:
        self.db.add(
            AuditEvent(
                actor_user_id=actor_user_id,
                action=action,
                target_type="resource_scope",
                target_id=scope.id,
                result="success",
                reason=None,
                changes_json=changes,
                request_id=request_id or request_id_context.get() or "unknown",
            )
        )

    def _now(self) -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)
