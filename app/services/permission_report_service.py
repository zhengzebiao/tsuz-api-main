from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session as DbSession

from app.core.logging import request_id_context
from app.models.app import App
from app.models.permission import Permission
from app.models.role import Role, role_permissions, user_roles
from app.schemas.internal_permission_report import PermissionReportItem
from app.services.session_service import SessionService

logger = logging.getLogger("app.permission_report")


class PermissionReportError(RuntimeError):
    code = "PERMISSION_REPORT_ERROR"


class PermissionReportCallerNotFoundError(PermissionReportError):
    code = "PERMISSION_REPORT_CALLER_NOT_FOUND"


class PermissionReportCallerDisabledError(PermissionReportError):
    code = "PERMISSION_REPORT_CALLER_DISABLED"


class PermissionReportAdminRoleRequiredError(PermissionReportError):
    code = "PERMISSION_REPORT_ADMIN_ROLE_REQUIRED"


class PermissionReportNameConflictError(PermissionReportError):
    code = "PERMISSION_NAME_CONFLICT"


@dataclass(frozen=True)
class PermissionReportSummary:
    caller_app_id: str
    created: int
    restored: int
    marked_missing: int
    admin_grants_added: int
    sessions_revoked: int
    unchanged: int


class PermissionReportService:
    """Synchronize one sub-application's complete declared permission set."""

    ADMIN_ROLE_NAME = "admin"
    SESSION_REVOCATION_REASON = "permission_report"
    ADVISORY_LOCK_KEY_NAMESPACE = "sub_app_permission_report"

    def __init__(
        self,
        db: DbSession,
        *,
        session_service: SessionService | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.db = db
        self.sessions = session_service or SessionService(db)
        self._now_factory = now or self._now

    def report(
        self,
        caller_app_id: str,
        items: list[PermissionReportItem],
        *,
        request_id: str | None = None,
    ) -> PermissionReportSummary:
        self._acquire_caller_lock(caller_app_id)
        caller = self._lock_caller(caller_app_id)
        admin_role = self._get_admin_role()
        existing = list(
            self.db.scalars(
                select(Permission)
                .where(Permission.owner_app_id == caller.app_id)
                .order_by(Permission.name, Permission.id)
            ).all()
        )
        existing_by_name = {permission.name: permission for permission in existing}
        submitted_by_name = {item.code: item for item in items}
        self._validate_name_ownership(submitted_by_name, caller.app_id)

        now = self._now_factory()
        created = 0
        restored = 0
        marked_missing = 0
        unchanged = 0
        effective_permission_ids: set[int] = set()
        for code, item in submitted_by_name.items():
            permission = existing_by_name.get(code)
            if permission is None:
                permission = Permission(
                    name=code,
                    owner_app_id=caller.app_id,
                    display_name=item.display_name,
                    description=item.description,
                    is_declared=True,
                    is_enabled=True,
                    created_at=now,
                    updated_at=now,
                    version=1,
                )
                self.db.add(permission)
                existing_by_name[code] = permission
                created += 1
                continue

            changed = False
            if not permission.is_declared or permission.missing_at is not None:
                permission.is_declared = True
                permission.missing_at = None
                permission.version += 1
                permission.updated_at = now
                restored += 1
                changed = True
                if permission.is_enabled:
                    effective_permission_ids.add(permission.id)
            if not changed:
                unchanged += 1

        submitted_codes = set(submitted_by_name)
        for permission in existing:
            if permission.name in submitted_codes:
                continue
            if permission.is_declared:
                permission.is_declared = False
                permission.missing_at = now
                permission.version += 1
                permission.updated_at = now
                marked_missing += 1
                if permission.is_enabled:
                    effective_permission_ids.add(permission.id)
            else:
                unchanged += 1

        self.db.flush()
        admin_permission_ids = set(
            self.db.scalars(
                select(role_permissions.c.permission_id).where(
                    role_permissions.c.role_id == admin_role.id
                )
            ).all()
        )
        admin_grants_added = 0
        for code in sorted(submitted_codes):
            permission = existing_by_name[code]
            if permission.id not in admin_permission_ids:
                self.db.execute(
                    role_permissions.insert().values(
                        role_id=admin_role.id,
                        permission_id=permission.id,
                    )
                )
                admin_grants_added += 1

        session_user_ids = self._affected_user_ids(
            admin_role_id=admin_role.id,
            permission_ids=effective_permission_ids,
            include_admin_users=bool(admin_grants_added),
        )
        sessions_revoked = sum(
            self.sessions.revoke_user_sessions(user_id, self.SESSION_REVOCATION_REASON)
            for user_id in sorted(session_user_ids)
        )
        self.db.flush()
        self._log_summary(
            caller_app_id=caller.app_id,
            request_id=request_id,
            created=created,
            restored=restored,
            marked_missing=marked_missing,
            admin_grants_added=admin_grants_added,
            sessions_revoked=sessions_revoked,
            unchanged=unchanged,
        )
        return PermissionReportSummary(
            caller_app_id=caller.app_id,
            created=created,
            restored=restored,
            marked_missing=marked_missing,
            admin_grants_added=admin_grants_added,
            sessions_revoked=sessions_revoked,
            unchanged=unchanged,
        )

    def _acquire_caller_lock(self, caller_app_id: str) -> None:
        if self.db.get_bind().dialect.name == "postgresql":
            self.db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:namespace), hashtext(:caller_app_id))"),
                {"namespace": self.ADVISORY_LOCK_KEY_NAMESPACE, "caller_app_id": caller_app_id},
            )

    def _lock_caller(self, caller_app_id: str) -> App:
        caller = self.db.scalar(
            select(App).where(App.app_id == caller_app_id).with_for_update()
        )
        if caller is None:
            raise PermissionReportCallerNotFoundError(PermissionReportCallerNotFoundError.code)
        if not caller.is_enabled:
            raise PermissionReportCallerDisabledError(PermissionReportCallerDisabledError.code)
        return caller

    def _get_admin_role(self) -> Role:
        role = self.db.scalar(
            select(Role).where(Role.name == self.ADMIN_ROLE_NAME).with_for_update()
        )
        if role is None:
            raise PermissionReportAdminRoleRequiredError(
                PermissionReportAdminRoleRequiredError.code
            )
        return role

    def _validate_name_ownership(
        self,
        submitted: dict[str, PermissionReportItem],
        caller_app_id: str,
    ) -> None:
        if not submitted:
            return
        conflicts = self.db.scalars(
            select(Permission.name).where(
                Permission.name.in_(submitted),
                Permission.owner_app_id.is_not(None),
                Permission.owner_app_id != caller_app_id,
            )
        ).all()
        if conflicts:
            raise PermissionReportNameConflictError(PermissionReportNameConflictError.code)

        main_owned = self.db.scalars(
            select(Permission.name).where(
                Permission.name.in_(submitted),
                Permission.owner_app_id.is_(None),
            )
        ).all()
        if main_owned:
            raise PermissionReportNameConflictError(PermissionReportNameConflictError.code)

    def _affected_user_ids(
        self,
        *,
        admin_role_id: int,
        permission_ids: set[int],
        include_admin_users: bool,
    ) -> set[int]:
        user_ids: set[int] = set()
        if permission_ids:
            user_ids.update(
                self.db.scalars(
                    select(user_roles.c.user_id)
                    .join(role_permissions, role_permissions.c.role_id == user_roles.c.role_id)
                    .join(Role, Role.id == user_roles.c.role_id)
                    .where(
                        role_permissions.c.permission_id.in_(permission_ids),
                        Role.is_enabled.is_(True),
                    )
                    .distinct()
                ).all()
            )
        if include_admin_users:
            user_ids.update(
                self.db.scalars(
                    select(user_roles.c.user_id)
                    .where(user_roles.c.role_id == admin_role_id)
                    .distinct()
                ).all()
            )
        return user_ids

    def _log_summary(self, *, caller_app_id: str, request_id: str | None, **counts: int) -> None:
        logger.info(
            "permission report completed caller_app_id=%s request_id=%s counts=%s",
            caller_app_id,
            request_id or request_id_context.get() or "unknown",
            counts,
        )

    def _now(self) -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)
