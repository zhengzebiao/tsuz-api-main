from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.core.logging import request_id_context
from app.models.audit_event import AuditEvent
from app.models.permission import Permission
from app.models.role import Role, role_permissions, user_roles
from app.models.user import User
from app.schemas.admin_role import AdminRoleCreate, AdminRoleUpdate
from app.services.admin_permission_service import (
    PermissionDisabledError,
    PermissionNotDeclaredError,
    PermissionNotFoundError,
)
from app.services.session_service import SessionService


class AdminRoleError(ValueError):
    code = "ADMIN_ROLE_ERROR"


class RoleNotFoundError(AdminRoleError):
    code = "ROLE_NOT_FOUND"


class RoleNameAlreadyExistsError(AdminRoleError):
    code = "ROLE_NAME_ALREADY_EXISTS"


class RoleVersionConflictError(AdminRoleError):
    code = "ROLE_VERSION_CONFLICT"


class ProtectedRoleOperationError(AdminRoleError):
    code = "PROTECTED_ROLE_OPERATION"


class RoleDisabledError(AdminRoleError):
    code = "ROLE_DISABLED"


class AdminRoleService:
    ADMIN_ROLE_NAME = "admin"

    def __init__(self, db: DbSession) -> None:
        self.db = db
        self.sessions = SessionService(db)

    def list_roles(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        keyword: str | None = None,
        is_enabled: bool | None = None,
    ) -> tuple[list[Role], int]:
        query = select(Role)
        count_query = select(func.count()).select_from(Role)
        filters: list[Any] = []
        if keyword:
            normalized_keyword = keyword.strip().lower()
            if normalized_keyword:
                filters.append(
                    or_(
                        func.lower(Role.name).contains(normalized_keyword),
                        func.lower(Role.description).contains(normalized_keyword),
                    )
                )
        if is_enabled is not None:
            filters.append(Role.is_enabled == is_enabled)
        if filters:
            query = query.where(*filters)
            count_query = count_query.where(*filters)
        total = self.db.scalar(count_query) or 0
        roles = self.db.scalars(
            query.order_by(Role.created_at.desc(), Role.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return list(roles), total

    def get_role(self, role_id: int) -> Role:
        role = self.db.get(Role, role_id)
        if role is None:
            raise RoleNotFoundError(RoleNotFoundError.code)
        return role

    def create_role(
        self,
        payload: AdminRoleCreate,
        *,
        actor_user_id: int,
        request_id: str | None = None,
    ) -> Role:
        if self.db.scalar(select(Role.id).where(Role.name == payload.name)) is not None:
            raise RoleNameAlreadyExistsError(RoleNameAlreadyExistsError.code)

        role = Role(
            name=payload.name,
            description=payload.description,
            is_enabled=True,
        )
        try:
            with self.db.begin_nested():
                self.db.add(role)
                self.db.flush()
        except IntegrityError as exc:
            if self.db.scalar(select(Role.id).where(Role.name == payload.name)) is not None:
                raise RoleNameAlreadyExistsError(RoleNameAlreadyExistsError.code) from None
            raise AdminRoleError(AdminRoleError.code) from exc

        self._add_audit(
            actor_user_id=actor_user_id,
            action="role.created",
            target_id=role.id,
            result="success",
            request_id=request_id,
            changes={
                "name": {"from": None, "to": role.name},
                "description": {"from": None, "to": role.description},
                "is_enabled": {"from": None, "to": role.is_enabled},
            },
        )
        self.db.flush()
        return role

    def update_role(
        self,
        role_id: int,
        payload: AdminRoleUpdate,
        *,
        actor_user_id: int,
        request_id: str | None = None,
    ) -> tuple[Role, bool, int]:
        current = self.get_role(role_id)
        if current.version != payload.version:
            raise RoleVersionConflictError(RoleVersionConflictError.code)

        submitted = payload.model_dump(exclude_unset=True, exclude={"version"})
        changes = {key: value for key, value in submitted.items() if getattr(current, key) != value}
        if not changes:
            return current, False, 0
        if current.name == self.ADMIN_ROLE_NAME and "name" in changes:
            raise ProtectedRoleOperationError(ProtectedRoleOperationError.code)

        before = {key: getattr(current, key) for key in changes}
        now = self._now()
        try:
            with self.db.begin_nested():
                result = self.db.execute(
                    update(Role)
                    .where(Role.id == role_id, Role.version == payload.version)
                    .values(**changes, version=Role.version + 1, updated_at=now)
                )
                self.db.flush()
        except IntegrityError as exc:
            if "name" in changes:
                raise RoleNameAlreadyExistsError(RoleNameAlreadyExistsError.code) from None
            raise AdminRoleError(AdminRoleError.code) from exc
        if result.rowcount != 1:
            if self.db.scalar(select(Role.id).where(Role.id == role_id)) is None:
                raise RoleNotFoundError(RoleNotFoundError.code)
            raise RoleVersionConflictError(RoleVersionConflictError.code)

        self.db.expire(current)
        self.db.refresh(current)
        revoked_sessions = 0
        if "name" in changes:
            revoked_sessions = self._revoke_role_user_sessions(role_id, "role_name_changed")
        self._add_audit(
            actor_user_id=actor_user_id,
            action="role.updated",
            target_id=current.id,
            result="success",
            request_id=request_id,
            changes={
                **{key: {"from": before[key], "to": getattr(current, key)} for key in changes},
                "revoked_sessions": revoked_sessions,
            },
        )
        self.db.flush()
        return current, True, revoked_sessions

    def disable_role(
        self,
        role_id: int,
        *,
        actor_user_id: int,
        reason: str | None = None,
        request_id: str | None = None,
    ) -> tuple[Role, bool, int]:
        role = self._lock_role(role_id)
        if role.name == self.ADMIN_ROLE_NAME:
            raise ProtectedRoleOperationError(ProtectedRoleOperationError.code)
        if not role.is_enabled:
            return role, False, 0

        reason = self._normalize_reason(reason)
        before = {
            "is_enabled": role.is_enabled,
            "disabled_at": role.disabled_at,
            "disabled_reason": role.disabled_reason,
        }
        now = self._now()
        role.is_enabled = False
        role.disabled_at = now
        role.disabled_reason = reason
        self._increment_version(role, now)
        revoked_sessions = self._revoke_role_user_sessions(role.id, "role_disabled")
        self._add_change_audit(
            role=role,
            action="role.disabled",
            actor_user_id=actor_user_id,
            reason=reason,
            request_id=request_id,
            before=before,
            revoked_sessions=revoked_sessions,
        )
        self.db.flush()
        return role, True, revoked_sessions

    def enable_role(
        self,
        role_id: int,
        *,
        actor_user_id: int,
        request_id: str | None = None,
    ) -> tuple[Role, bool, int]:
        role = self._lock_role(role_id)
        if role.is_enabled:
            return role, False, 0

        before = {
            "is_enabled": role.is_enabled,
            "disabled_at": role.disabled_at,
            "disabled_reason": role.disabled_reason,
        }
        now = self._now()
        role.is_enabled = True
        role.disabled_at = None
        role.disabled_reason = None
        self._increment_version(role, now)
        self._add_change_audit(
            role=role,
            action="role.enabled",
            actor_user_id=actor_user_id,
            reason=None,
            request_id=request_id,
            before=before,
            revoked_sessions=0,
        )
        self.db.flush()
        return role, True, 0

    def get_role_permissions(self, role_id: int) -> list[Permission]:
        self.get_role(role_id)
        return self._get_permissions_for_role(role_id)

    def assign_permissions(
        self,
        role_id: int,
        permission_ids: list[int],
        version: int,
        *,
        actor_user_id: int,
        request_id: str | None = None,
    ) -> tuple[Role, list[Permission], bool, int]:
        role = self._lock_role(role_id)
        if role.version != version:
            raise RoleVersionConflictError(RoleVersionConflictError.code)

        current_permissions = self._get_permissions_for_role(role_id)
        current_by_id = {
            permission.id: permission for permission in current_permissions
        }
        target_ids = set(permission_ids)
        target_permissions = self._get_permissions_by_ids(target_ids)
        target_by_id = {
            permission.id: permission for permission in target_permissions
        }
        if set(target_by_id) != target_ids:
            raise PermissionNotFoundError(PermissionNotFoundError.code)

        current_ids = set(current_by_id)
        added_ids = target_ids - current_ids
        removed_ids = current_ids - target_ids
        for permission_id in sorted(added_ids):
            permission = target_by_id[permission_id]
            if not permission.is_declared:
                raise PermissionNotDeclaredError(PermissionNotDeclaredError.code)
            if not permission.is_enabled:
                raise PermissionDisabledError(PermissionDisabledError.code)
        if not added_ids and not removed_ids:
            return role, current_permissions, False, 0

        if role.name == self.ADMIN_ROLE_NAME and any(
            current_by_id[permission_id].is_declared
            and current_by_id[permission_id].is_enabled
            for permission_id in removed_ids
        ):
            raise ProtectedRoleOperationError(ProtectedRoleOperationError.code)

        before_permissions = self._permission_audit_values(current_permissions)
        if removed_ids:
            self.db.execute(
                delete(role_permissions).where(
                    role_permissions.c.role_id == role_id,
                    role_permissions.c.permission_id.in_(removed_ids),
                )
            )
        if added_ids:
            self.db.execute(
                role_permissions.insert(),
                [
                    {"role_id": role_id, "permission_id": permission_id}
                    for permission_id in sorted(added_ids)
                ],
            )

        now = self._now()
        self._increment_version(role, now)
        self.db.flush()
        revoked_sessions = self._revoke_role_user_sessions(
            role_id,
            "role_permissions_changed",
        )
        assigned_permissions = sorted(
            target_permissions,
            key=lambda permission: (permission.name, permission.id),
        )
        self._add_audit(
            actor_user_id=actor_user_id,
            action="role.permissions_assigned",
            target_id=role_id,
            result="success",
            request_id=request_id,
            changes={
                "permissions": {
                    "from": before_permissions,
                    "to": self._permission_audit_values(assigned_permissions),
                },
                "revoked_sessions": revoked_sessions,
            },
        )
        self.db.flush()
        return role, assigned_permissions, True, revoked_sessions

    def list_role_users(
        self,
        role_id: int,
        *,
        page: int = 1,
        page_size: int = 20,
        keyword: str | None = None,
        is_active: bool | None = None,
        is_blacklisted: bool | None = None,
    ) -> tuple[list[User], int]:
        self.get_role(role_id)
        query = select(User).join(user_roles, user_roles.c.user_id == User.id).where(user_roles.c.role_id == role_id)
        count_query = (
            select(func.count())
            .select_from(User)
            .join(user_roles, user_roles.c.user_id == User.id)
            .where(user_roles.c.role_id == role_id)
        )
        filters: list[Any] = []
        if keyword:
            normalized_keyword = keyword.strip().lower()
            if normalized_keyword:
                filters.append(
                    or_(
                        func.lower(User.email).contains(normalized_keyword),
                        func.lower(User.display_name).contains(normalized_keyword),
                    )
                )
        if is_active is not None:
            filters.append(User.is_active == is_active)
        if is_blacklisted is not None:
            filters.append(User.is_blacklisted == is_blacklisted)
        if filters:
            query = query.where(*filters)
            count_query = count_query.where(*filters)
        total = self.db.scalar(count_query) or 0
        users = self.db.scalars(
            query.order_by(User.id).offset((page - 1) * page_size).limit(page_size)
        ).all()
        return list(users), total

    def _lock_role(self, role_id: int) -> Role:
        role = self.db.scalar(select(Role).where(Role.id == role_id).with_for_update())
        if role is None:
            raise RoleNotFoundError(RoleNotFoundError.code)
        return role

    def _get_permissions_for_role(self, role_id: int) -> list[Permission]:
        return list(
            self.db.scalars(
                select(Permission)
                .join(
                    role_permissions,
                    role_permissions.c.permission_id == Permission.id,
                )
                .where(role_permissions.c.role_id == role_id)
                .order_by(Permission.name, Permission.id)
            ).all()
        )

    def _get_permissions_by_ids(
        self,
        permission_ids: set[int],
    ) -> list[Permission]:
        if not permission_ids:
            return []
        return list(
            self.db.scalars(
                select(Permission)
                .where(Permission.id.in_(permission_ids))
                .order_by(Permission.id)
                .with_for_update()
            ).all()
        )

    def _permission_audit_values(
        self,
        permissions: list[Permission],
    ) -> list[dict[str, int | str]]:
        return [
            {"id": permission.id, "name": permission.name}
            for permission in sorted(
                permissions,
                key=lambda permission: (permission.name, permission.id),
            )
        ]

    def _revoke_role_user_sessions(self, role_id: int, reason: str) -> int:
        user_ids = self.db.scalars(
            select(user_roles.c.user_id)
            .where(user_roles.c.role_id == role_id)
            .order_by(user_roles.c.user_id)
        ).all()
        return sum(self.sessions.revoke_user_sessions(user_id, reason) for user_id in user_ids)

    def _add_change_audit(
        self,
        *,
        role: Role,
        action: str,
        actor_user_id: int,
        reason: str | None,
        request_id: str | None,
        before: dict[str, Any],
        revoked_sessions: int,
    ) -> None:
        changes = {key: {"from": value, "to": getattr(role, key)} for key, value in before.items()}
        changes["revoked_sessions"] = revoked_sessions
        self._add_audit(
            actor_user_id=actor_user_id,
            action=action,
            target_id=role.id,
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
                target_type="role",
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

    def _increment_version(self, role: Role, now: datetime) -> None:
        role.version += 1
        role.updated_at = now

    def _normalize_reason(self, reason: str | None) -> str | None:
        if reason is None:
            return None
        normalized = reason.strip()
        if len(normalized) > 500:
            raise ValueError("invalid reason")
        return normalized or None

    def _now(self) -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)
