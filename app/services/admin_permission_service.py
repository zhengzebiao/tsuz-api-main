from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session as DbSession

from app.core.logging import request_id_context
from app.models.audit_event import AuditEvent
from app.models.permission import Permission
from app.models.permission_endpoint import PermissionEndpoint
from app.models.role import Role, role_permissions, user_roles
from app.schemas.admin_permission import AdminPermissionUpdate
from app.services.session_service import SessionService


class AdminPermissionError(ValueError):
    code = "ADMIN_PERMISSION_ERROR"


class PermissionNotFoundError(AdminPermissionError):
    code = "PERMISSION_NOT_FOUND"


class PermissionVersionConflictError(AdminPermissionError):
    code = "PERMISSION_VERSION_CONFLICT"


class ProtectedPermissionOperationError(AdminPermissionError):
    code = "PROTECTED_PERMISSION_OPERATION"


class PermissionNotDeclaredError(AdminPermissionError):
    code = "PERMISSION_NOT_DECLARED"


@dataclass(frozen=True)
class PermissionRecord:
    permission: Permission
    endpoint_count: int
    role_count: int
    endpoints: tuple[PermissionEndpoint, ...] = ()


class AdminPermissionService:
    CORE_ENABLE_PERMISSION = "permission:enable"
    RESOURCE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

    def __init__(self, db: DbSession) -> None:
        self.db = db
        self.sessions = SessionService(db)

    def list_permissions(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        keyword: str | None = None,
        resource: str | None = None,
        is_declared: bool | None = None,
        is_enabled: bool | None = None,
    ) -> tuple[list[PermissionRecord], int]:
        endpoint_count = self._endpoint_count_expression()
        role_count = self._role_count_expression()
        query = select(
            Permission,
            endpoint_count.label("endpoint_count"),
            role_count.label("role_count"),
        )
        count_query = select(func.count()).select_from(Permission)
        filters: list[Any] = []

        if keyword:
            normalized_keyword = keyword.strip().lower()
            if normalized_keyword:
                filters.append(
                    or_(
                        func.lower(Permission.name).contains(normalized_keyword),
                        func.lower(Permission.display_name).contains(normalized_keyword),
                        func.lower(Permission.description).contains(normalized_keyword),
                    )
                )
        if resource is not None:
            normalized_resource = resource.strip()
            if self.RESOURCE_PATTERN.fullmatch(normalized_resource) is None:
                raise ValueError("invalid resource")
            filters.append(
                Permission.name.startswith(f"{normalized_resource}:", autoescape=True)
            )
        if is_declared is not None:
            filters.append(Permission.is_declared.is_(is_declared))
        if is_enabled is not None:
            filters.append(Permission.is_enabled.is_(is_enabled))

        if filters:
            query = query.where(*filters)
            count_query = count_query.where(*filters)
        total = self.db.scalar(count_query) or 0
        rows = self.db.execute(
            query.order_by(Permission.name, Permission.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        records = [
            PermissionRecord(
                permission=permission,
                endpoint_count=int(endpoint_count_value),
                role_count=int(role_count_value),
            )
            for permission, endpoint_count_value, role_count_value in rows
        ]
        return records, total

    def get_permission(self, permission_id: int) -> PermissionRecord:
        permission = self._get_permission(permission_id)
        return self._record(permission, include_endpoints=True)

    def update_permission(
        self,
        permission_id: int,
        payload: AdminPermissionUpdate,
        *,
        actor_user_id: int,
        request_id: str | None = None,
    ) -> tuple[PermissionRecord, bool, int]:
        current = self._get_permission(permission_id)
        if current.version != payload.version:
            raise PermissionVersionConflictError(PermissionVersionConflictError.code)

        submitted = payload.model_dump(exclude_unset=True, exclude={"version"})
        changes = {key: value for key, value in submitted.items() if getattr(current, key) != value}
        if not changes:
            return self._record(current), False, 0

        before = {key: getattr(current, key) for key in changes}
        result = self.db.execute(
            update(Permission)
            .where(Permission.id == permission_id, Permission.version == payload.version)
            .values(**changes, version=Permission.version + 1, updated_at=self._now())
        )
        if result.rowcount != 1:
            if self.db.scalar(select(Permission.id).where(Permission.id == permission_id)) is None:
                raise PermissionNotFoundError(PermissionNotFoundError.code)
            raise PermissionVersionConflictError(PermissionVersionConflictError.code)

        self.db.refresh(current)
        self._add_audit(
            actor_user_id=actor_user_id,
            action="permission.updated",
            target_id=current.id,
            result="success",
            request_id=request_id,
            changes={key: {"from": before[key], "to": getattr(current, key)} for key in changes},
        )
        self.db.flush()
        return self._record(current), True, 0

    def disable_permission(
        self,
        permission_id: int,
        *,
        actor_user_id: int,
        reason: str | None = None,
        request_id: str | None = None,
    ) -> tuple[PermissionRecord, bool, int]:
        permission = self._lock_permission(permission_id)
        if permission.name == self.CORE_ENABLE_PERMISSION:
            raise ProtectedPermissionOperationError(ProtectedPermissionOperationError.code)
        if not permission.is_enabled:
            return self._record(permission), False, 0

        normalized_reason = self._normalize_reason(reason)
        before = {
            "is_enabled": permission.is_enabled,
            "disabled_at": permission.disabled_at,
            "disabled_reason": permission.disabled_reason,
        }
        now = self._now()
        permission.is_enabled = False
        permission.disabled_at = now
        permission.disabled_reason = normalized_reason
        self._increment_version(permission, now)
        revoked_sessions = self._revoke_permission_user_sessions(permission.id)
        self._add_change_audit(
            permission=permission,
            action="permission.disabled",
            actor_user_id=actor_user_id,
            reason=normalized_reason,
            request_id=request_id,
            before=before,
            revoked_sessions=revoked_sessions,
        )
        self.db.flush()
        return self._record(permission), True, revoked_sessions

    def enable_permission(
        self,
        permission_id: int,
        *,
        actor_user_id: int,
        request_id: str | None = None,
    ) -> tuple[PermissionRecord, bool, int]:
        permission = self._lock_permission(permission_id)
        if not permission.is_declared:
            raise PermissionNotDeclaredError(PermissionNotDeclaredError.code)
        if permission.is_enabled:
            return self._record(permission), False, 0

        before = {
            "is_enabled": permission.is_enabled,
            "disabled_at": permission.disabled_at,
            "disabled_reason": permission.disabled_reason,
        }
        now = self._now()
        permission.is_enabled = True
        permission.disabled_at = None
        permission.disabled_reason = None
        self._increment_version(permission, now)
        self._add_change_audit(
            permission=permission,
            action="permission.enabled",
            actor_user_id=actor_user_id,
            reason=None,
            request_id=request_id,
            before=before,
            revoked_sessions=0,
        )
        self.db.flush()
        return self._record(permission), True, 0

    def _get_permission(self, permission_id: int) -> Permission:
        permission = self.db.get(Permission, permission_id)
        if permission is None:
            raise PermissionNotFoundError(PermissionNotFoundError.code)
        return permission

    def _lock_permission(self, permission_id: int) -> Permission:
        permission = self.db.scalar(
            select(Permission).where(Permission.id == permission_id).with_for_update()
        )
        if permission is None:
            raise PermissionNotFoundError(PermissionNotFoundError.code)
        return permission

    def _record(self, permission: Permission, *, include_endpoints: bool = False) -> PermissionRecord:
        endpoint_count = self.db.scalar(
            select(func.count())
            .select_from(PermissionEndpoint)
            .where(PermissionEndpoint.permission_id == permission.id)
        ) or 0
        role_count = self.db.scalar(
            select(func.count())
            .select_from(role_permissions)
            .where(role_permissions.c.permission_id == permission.id)
        ) or 0
        endpoints: tuple[PermissionEndpoint, ...] = ()
        if include_endpoints:
            endpoints = tuple(
                self.db.scalars(
                    select(PermissionEndpoint)
                    .where(PermissionEndpoint.permission_id == permission.id)
                    .order_by(
                        PermissionEndpoint.http_method,
                        PermissionEndpoint.path,
                        PermissionEndpoint.route_name,
                    )
                ).all()
            )
        return PermissionRecord(
            permission=permission,
            endpoint_count=int(endpoint_count),
            role_count=int(role_count),
            endpoints=endpoints,
        )

    def _endpoint_count_expression(self):
        return (
            select(func.count())
            .select_from(PermissionEndpoint)
            .where(PermissionEndpoint.permission_id == Permission.id)
            .correlate(Permission)
            .scalar_subquery()
        )

    def _role_count_expression(self):
        return (
            select(func.count())
            .select_from(role_permissions)
            .where(role_permissions.c.permission_id == Permission.id)
            .correlate(Permission)
            .scalar_subquery()
        )

    def _revoke_permission_user_sessions(self, permission_id: int) -> int:
        user_ids = self.db.scalars(
            select(user_roles.c.user_id)
            .join(role_permissions, role_permissions.c.role_id == user_roles.c.role_id)
            .join(Role, Role.id == user_roles.c.role_id)
            .where(
                role_permissions.c.permission_id == permission_id,
                Role.is_enabled.is_(True),
            )
            .distinct()
            .order_by(user_roles.c.user_id)
        ).all()
        return sum(
            self.sessions.revoke_user_sessions(user_id, "permission_disabled")
            for user_id in user_ids
        )

    def _add_change_audit(
        self,
        *,
        permission: Permission,
        action: str,
        actor_user_id: int,
        reason: str | None,
        request_id: str | None,
        before: dict[str, Any],
        revoked_sessions: int,
    ) -> None:
        changes = {
            key: {"from": value, "to": getattr(permission, key)}
            for key, value in before.items()
        }
        changes["revoked_sessions"] = revoked_sessions
        self._add_audit(
            actor_user_id=actor_user_id,
            action=action,
            target_id=permission.id,
            result="success",
            reason=reason,
            request_id=request_id,
            changes=changes,
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
                target_type="permission",
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

    def _increment_version(self, permission: Permission, now: datetime) -> None:
        permission.version += 1
        permission.updated_at = now

    def _normalize_reason(self, reason: str | None) -> str | None:
        if reason is None:
            return None
        normalized = reason.strip()
        if len(normalized) > 500:
            raise ValueError("invalid reason")
        return normalized or None

    def _now(self) -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)
