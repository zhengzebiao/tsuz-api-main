from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.core.logging import request_id_context
from app.models.app import App
from app.models.app_service_grant import AppServiceGrant
from app.models.audit_event import AuditEvent
from app.models.resource_scope import ResourceScope
from app.schemas.service_authorization import AppServiceGrantCreate


class AppServiceGrantError(ValueError):
    code = "APP_SERVICE_GRANT_ERROR"


class AppServiceGrantNotFoundError(AppServiceGrantError):
    code = "APP_SERVICE_GRANT_NOT_FOUND"


class AppServiceGrantCallerNotFoundError(AppServiceGrantError):
    code = "APP_SERVICE_GRANT_CALLER_NOT_FOUND"


class AppServiceGrantScopeNotFoundError(AppServiceGrantError):
    code = "APP_SERVICE_GRANT_SCOPE_NOT_FOUND"


class AppServiceGrantRevokedError(AppServiceGrantError):
    code = "APP_SERVICE_GRANT_REVOKED"


class AppServiceGrantAlreadyExistsError(AppServiceGrantError):
    code = "APP_SERVICE_GRANT_ALREADY_EXISTS"


@dataclass(frozen=True)
class AppServiceGrantRecord:
    grant: AppServiceGrant
    target_app_id: str
    scope_code: str


class AppServiceGrantService:
    def __init__(self, db: DbSession) -> None:
        self.db = db

    def list_grants(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        caller_app_id: str | None = None,
        target_app_id: str | None = None,
        status: str | None = None,
    ) -> tuple[list[AppServiceGrantRecord], int]:
        query = select(AppServiceGrant, ResourceScope).join(
            ResourceScope,
            ResourceScope.id == AppServiceGrant.scope_id,
        )
        count_query = (
            select(func.count())
            .select_from(AppServiceGrant)
            .join(ResourceScope, ResourceScope.id == AppServiceGrant.scope_id)
        )
        filters: list[Any] = []
        if caller_app_id:
            filters.append(AppServiceGrant.caller_app_id == caller_app_id.strip())
        if target_app_id:
            filters.append(ResourceScope.target_app_id == target_app_id.strip())
        if status:
            filters.append(AppServiceGrant.status == status)
        if filters:
            query = query.where(*filters)
            count_query = count_query.where(*filters)
        total = self.db.scalar(count_query) or 0
        rows = self.db.execute(
            query.order_by(AppServiceGrant.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [self._record(grant, scope) for grant, scope in rows], int(total)

    def create_grant(
        self,
        payload: AppServiceGrantCreate,
        *,
        actor_user_id: int,
        request_id: str | None = None,
    ) -> tuple[AppServiceGrantRecord, bool]:
        if self.db.scalar(select(App.id).where(App.app_id == payload.caller_app_id)) is None:
            raise AppServiceGrantCallerNotFoundError(AppServiceGrantCallerNotFoundError.code)
        scope = self.db.get(ResourceScope, payload.scope_id)
        if scope is None:
            raise AppServiceGrantScopeNotFoundError(AppServiceGrantScopeNotFoundError.code)

        existing = self.db.scalar(
            select(AppServiceGrant).where(
                AppServiceGrant.caller_app_id == payload.caller_app_id,
                AppServiceGrant.scope_id == payload.scope_id,
            )
        )
        if existing is not None:
            if existing.status == "revoked":
                raise AppServiceGrantRevokedError(AppServiceGrantRevokedError.code)
            if existing.valid_from == (payload.valid_from or existing.valid_from) and existing.expires_at == payload.expires_at:
                return self._record(existing, scope), False
            raise AppServiceGrantAlreadyExistsError(AppServiceGrantAlreadyExistsError.code)

        grant = AppServiceGrant(
            caller_app_id=payload.caller_app_id,
            scope_id=scope.id,
            status="enabled",
            valid_from=payload.valid_from or self._now(),
            expires_at=payload.expires_at,
            created_by=actor_user_id,
        )
        if grant.expires_at is not None and grant.expires_at <= grant.valid_from:
            raise ValueError("expires_at must be later than valid_from")
        try:
            with self.db.begin_nested():
                self.db.add(grant)
                self.db.flush()
        except IntegrityError:
            raise AppServiceGrantAlreadyExistsError(AppServiceGrantAlreadyExistsError.code) from None
        self._add_audit(
            grant=grant,
            action="app_service_grant.created",
            actor_user_id=actor_user_id,
            request_id=request_id,
            changes={
                "caller_app_id": {"from": None, "to": grant.caller_app_id},
                "scope_id": {"from": None, "to": grant.scope_id},
                "status": {"from": None, "to": grant.status},
                "valid_from": {"from": None, "to": grant.valid_from.isoformat()},
                "expires_at": {
                    "from": None,
                    "to": grant.expires_at.isoformat() if grant.expires_at is not None else None,
                },
            },
        )
        self.db.flush()
        return self._record(grant, scope), True

    def revoke_grant(
        self,
        grant_id: int,
        *,
        actor_user_id: int,
        reason: str | None = None,
        request_id: str | None = None,
    ) -> tuple[AppServiceGrantRecord, bool]:
        row = self.db.execute(
            select(AppServiceGrant, ResourceScope)
            .join(ResourceScope, ResourceScope.id == AppServiceGrant.scope_id)
            .where(AppServiceGrant.id == grant_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise AppServiceGrantNotFoundError(AppServiceGrantNotFoundError.code)
        grant, scope = row
        if grant.status == "revoked":
            return self._record(grant, scope), False
        now = self._now()
        grant.status = "revoked"
        grant.revoked_by = actor_user_id
        grant.revoked_at = now
        grant.revoke_reason = reason
        self._add_audit(
            grant=grant,
            action="app_service_grant.revoked",
            actor_user_id=actor_user_id,
            request_id=request_id,
            reason=reason,
            changes={
                "status": {"from": "enabled", "to": "revoked"},
                "revoked_at": {"from": None, "to": now.isoformat()},
            },
        )
        self.db.flush()
        return self._record(grant, scope), True

    def effective_scopes(
        self,
        *,
        caller_app_id: str,
        target_app_id: str,
        now: datetime | None = None,
    ) -> set[str]:
        current_time = now or self._now()
        scopes = self.db.scalars(
            select(ResourceScope.scope_code)
            .join(AppServiceGrant, AppServiceGrant.scope_id == ResourceScope.id)
            .where(
                AppServiceGrant.caller_app_id == caller_app_id,
                AppServiceGrant.status == "enabled",
                AppServiceGrant.valid_from <= current_time,
                or_(
                    AppServiceGrant.expires_at.is_(None),
                    AppServiceGrant.expires_at > current_time,
                ),
                ResourceScope.target_app_id == target_app_id,
                ResourceScope.is_enabled.is_(True),
            )
        ).all()
        return set(scopes)

    def _record(self, grant: AppServiceGrant, scope: ResourceScope) -> AppServiceGrantRecord:
        return AppServiceGrantRecord(
            grant=grant,
            target_app_id=scope.target_app_id,
            scope_code=scope.scope_code,
        )

    def _add_audit(
        self,
        *,
        grant: AppServiceGrant,
        action: str,
        actor_user_id: int,
        request_id: str | None,
        changes: dict[str, Any],
        reason: str | None = None,
    ) -> None:
        self.db.add(
            AuditEvent(
                actor_user_id=actor_user_id,
                action=action,
                target_type="app_service_grant",
                target_id=grant.id,
                result="success",
                reason=reason,
                changes_json=changes,
                request_id=request_id or request_id_context.get() or "unknown",
            )
        )

    def _now(self) -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)
