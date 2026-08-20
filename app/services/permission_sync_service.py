from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session as DbSession

from app.models.permission import Permission
from app.models.permission_endpoint import PermissionEndpoint
from app.models.role import Role, role_permissions, user_roles
from app.services.permission_scanner import (
    PermissionScanResult,
    ScannedPermissionBinding,
)
from app.services.session_service import SessionService


class PermissionSyncError(RuntimeError):
    """Base error for permission synchronization configuration or execution."""


class AdminRoleRequiredError(PermissionSyncError):
    """Raised when synchronization runs before the admin role is seeded."""


class PermissionSyncDialectError(PermissionSyncError):
    """Raised when a write synchronization is attempted outside PostgreSQL."""


@dataclass(frozen=True)
class PermissionSyncPlan:
    """A deterministic, database-only description of one synchronization."""

    scan_result: PermissionScanResult
    created: tuple[str, ...]
    restored: tuple[str, ...]
    marked_missing: tuple[str, ...]
    endpoint_bindings_added: tuple[ScannedPermissionBinding, ...]
    endpoint_bindings_removed: tuple[ScannedPermissionBinding, ...]
    admin_grants_added: tuple[str, ...]
    unchanged: tuple[str, ...]
    permission_updates: tuple[str, ...]
    session_user_ids: tuple[int, ...]

    @property
    def has_changes(self) -> bool:
        return bool(
            self.created
            or self.restored
            or self.marked_missing
            or self.endpoint_bindings_added
            or self.endpoint_bindings_removed
            or self.admin_grants_added
            or self.permission_updates
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": list(self.created),
            "restored": list(self.restored),
            "marked_missing": list(self.marked_missing),
            "endpoint_bindings_added": [
                _binding_to_dict(binding) for binding in self.endpoint_bindings_added
            ],
            "endpoint_bindings_removed": [
                _binding_to_dict(binding) for binding in self.endpoint_bindings_removed
            ],
            "admin_grants_added": list(self.admin_grants_added),
            "unchanged": list(self.unchanged),
            "session_users_affected": len(self.session_user_ids),
            "counts": {
                "created": len(self.created),
                "restored": len(self.restored),
                "marked_missing": len(self.marked_missing),
                "endpoint_bindings_added": len(self.endpoint_bindings_added),
                "endpoint_bindings_removed": len(self.endpoint_bindings_removed),
                "admin_grants_added": len(self.admin_grants_added),
                "unchanged": len(self.unchanged),
            },
            "has_changes": self.has_changes,
        }


@dataclass(frozen=True)
class PermissionSyncSummary:
    """Counts and safe details returned after a synchronization attempt."""

    created: int
    restored: int
    marked_missing: int
    endpoint_bindings_added: int
    endpoint_bindings_removed: int
    admin_grants_added: int
    sessions_revoked: int
    unchanged: int

    def to_dict(self) -> dict[str, int]:
        return {
            "created": self.created,
            "restored": self.restored,
            "marked_missing": self.marked_missing,
            "endpoint_bindings_added": self.endpoint_bindings_added,
            "endpoint_bindings_removed": self.endpoint_bindings_removed,
            "admin_grants_added": self.admin_grants_added,
            "sessions_revoked": self.sessions_revoked,
            "unchanged": self.unchanged,
        }


class PermissionSyncService:
    """Synchronize scanned permission declarations into PostgreSQL."""

    ADMIN_ROLE_NAME = "admin"
    ADVISORY_LOCK_KEY = 7_254_019_867
    SESSION_REVOCATION_REASON = "permission_sync"

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

    def build_plan(self, scan_result: PermissionScanResult) -> PermissionSyncPlan:
        self._validate_scan_result(scan_result)
        admin_role = self._get_admin_role()
        permissions = list(
            self.db.scalars(select(Permission).order_by(Permission.name, Permission.id)).all()
        )
        permission_by_name = {permission.name: permission for permission in permissions}
        target_names = set(scan_result.permission_names)
        target_bindings = _bindings_by_permission(scan_result.bindings)
        current_bindings = self._current_bindings()

        created: list[str] = []
        restored: list[str] = []
        marked_missing: list[str] = []
        permission_updates: set[str] = set()
        effective_permission_changes: set[str] = set()

        for name in sorted(target_names):
            permission = permission_by_name.get(name)
            if permission is None:
                created.append(name)
                permission_updates.add(name)
                continue

            status_changed = not permission.is_declared or permission.missing_at is not None
            if not permission.is_declared:
                restored.append(name)
                if permission.is_enabled:
                    effective_permission_changes.add(name)
            if status_changed:
                permission_updates.add(name)

        for name in sorted(permission_by_name.keys() - target_names):
            permission = permission_by_name[name]
            status_changed = permission.is_declared or permission.missing_at is None
            if permission.is_declared:
                marked_missing.append(name)
                if permission.is_enabled:
                    effective_permission_changes.add(name)
            if status_changed:
                permission_updates.add(name)

        endpoint_bindings_added: set[ScannedPermissionBinding] = set()
        endpoint_bindings_removed: set[ScannedPermissionBinding] = set()
        all_permission_names = set(permission_by_name) | target_names
        for name in sorted(all_permission_names):
            current = current_bindings.get(name, set())
            desired = target_bindings.get(name, set()) if name in target_names else set()
            endpoint_bindings_added.update(desired - current)
            endpoint_bindings_removed.update(current - desired)
            if current != desired:
                permission_updates.add(name)

        admin_permission_ids = set(
            self.db.scalars(
                select(role_permissions.c.permission_id).where(
                    role_permissions.c.role_id == admin_role.id
                )
            ).all()
        )
        admin_grants_added: list[str] = []
        for name in sorted(target_names):
            permission = permission_by_name.get(name)
            if permission is None or permission.id not in admin_permission_ids:
                admin_grants_added.append(name)

        unchanged = sorted(
            name
            for name, permission in permission_by_name.items()
            if name not in permission_updates and name not in admin_grants_added
        )
        session_user_ids = self._session_user_ids(
            permission_by_name=permission_by_name,
            admin_role_id=admin_role.id,
            effective_permission_changes=effective_permission_changes,
            admin_grants_added=set(admin_grants_added),
        )

        return PermissionSyncPlan(
            scan_result=scan_result,
            created=tuple(created),
            restored=tuple(restored),
            marked_missing=tuple(marked_missing),
            endpoint_bindings_added=tuple(sorted(endpoint_bindings_added)),
            endpoint_bindings_removed=tuple(sorted(endpoint_bindings_removed)),
            admin_grants_added=tuple(admin_grants_added),
            unchanged=tuple(unchanged),
            permission_updates=tuple(sorted(permission_updates)),
            session_user_ids=tuple(sorted(session_user_ids)),
        )

    def apply_plan(self, plan: PermissionSyncPlan) -> PermissionSyncSummary:
        """Apply a plan after taking the transaction-scoped PostgreSQL lock.

        The plan is intentionally rebuilt after the lock is acquired. A second
        deployment therefore observes the first deployment's committed state
        rather than applying a stale pre-lock diff.
        """

        self._acquire_advisory_lock()
        locked_plan = self.build_plan(plan.scan_result)
        return self._apply_locked_plan(locked_plan)

    def _apply_locked_plan(self, plan: PermissionSyncPlan) -> PermissionSyncSummary:
        permissions = {
            permission.name: permission
            for permission in self.db.scalars(select(Permission)).all()
        }
        now = self._now_factory()

        for name in plan.created:
            permission = Permission(
                name=name,
                display_name=name,
                description="",
                is_declared=True,
                is_enabled=True,
                created_at=now,
                updated_at=now,
                version=1,
            )
            self.db.add(permission)
            permissions[name] = permission
        self.db.flush()

        for name in plan.permission_updates:
            permission = permissions[name]
            if name in plan.scan_result.permission_names:
                permission.is_declared = True
                permission.missing_at = None
            else:
                permission.is_declared = False
                if permission.missing_at is None:
                    permission.missing_at = now
            if name not in plan.created:
                permission.version += 1
                permission.updated_at = now

        for binding in plan.endpoint_bindings_removed:
            permission = permissions.get(binding.permission_name)
            if permission is None:
                raise PermissionSyncError(
                    f"permission endpoint references unknown permission {binding.permission_name!r}"
                )
            self.db.execute(
                delete(PermissionEndpoint).where(
                    PermissionEndpoint.permission_id == permission.id,
                    PermissionEndpoint.http_method == binding.http_method,
                    PermissionEndpoint.path == binding.path,
                )
            )

        self.db.flush()
        for binding in plan.endpoint_bindings_added:
            permission = permissions.get(binding.permission_name)
            if permission is None:
                raise PermissionSyncError(
                    f"permission endpoint references unknown permission {binding.permission_name!r}"
                )
            self.db.add(
                PermissionEndpoint(
                    permission_id=permission.id,
                    http_method=binding.http_method,
                    path=binding.path,
                    route_name=binding.route_name,
                )
            )

        admin_role = self._get_admin_role()
        for name in plan.admin_grants_added:
            permission = permissions[name]
            self.db.execute(
                role_permissions.insert().values(
                    role_id=admin_role.id,
                    permission_id=permission.id,
                )
            )
        self.db.flush()

        sessions_revoked = 0
        for user_id in plan.session_user_ids:
            sessions_revoked += self.sessions.revoke_user_sessions(
                user_id,
                self.SESSION_REVOCATION_REASON,
            )

        return PermissionSyncSummary(
            created=len(plan.created),
            restored=len(plan.restored),
            marked_missing=len(plan.marked_missing),
            endpoint_bindings_added=len(plan.endpoint_bindings_added),
            endpoint_bindings_removed=len(plan.endpoint_bindings_removed),
            admin_grants_added=len(plan.admin_grants_added),
            sessions_revoked=sessions_revoked,
            unchanged=len(plan.unchanged),
        )

    def _acquire_advisory_lock(self) -> None:
        bind = self.db.get_bind()
        if bind.dialect.name != "postgresql":
            raise PermissionSyncDialectError(
                "permission synchronization requires a PostgreSQL database"
            )
        self.db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": self.ADVISORY_LOCK_KEY},
        )

    def _get_admin_role(self) -> Role:
        role = self.db.scalar(select(Role).where(Role.name == self.ADMIN_ROLE_NAME))
        if role is None:
            raise AdminRoleRequiredError(
                "admin role must exist before permission synchronization"
            )
        return role

    def _current_bindings(self) -> dict[str, set[ScannedPermissionBinding]]:
        rows = self.db.execute(
            select(
                Permission.name,
                PermissionEndpoint.http_method,
                PermissionEndpoint.path,
                PermissionEndpoint.route_name,
            )
            .join(
                PermissionEndpoint,
                PermissionEndpoint.permission_id == Permission.id,
            )
        ).all()
        return {
            name: {
                ScannedPermissionBinding(
                    permission_name=name,
                    http_method=http_method,
                    path=path,
                    route_name=route_name,
                )
                for http_method, path, route_name in group
            }
            for name, group in _group_binding_rows(rows).items()
        }

    def _session_user_ids(
        self,
        *,
        permission_by_name: dict[str, Permission],
        admin_role_id: int,
        effective_permission_changes: set[str],
        admin_grants_added: Iterable[str],
    ) -> set[int]:
        permission_ids = [
            permission_by_name[name].id
            for name in effective_permission_changes
            if name in permission_by_name
        ]
        user_ids: set[int] = set()
        if permission_ids:
            user_ids.update(
                self.db.scalars(
                    select(user_roles.c.user_id)
                    .join(Role, Role.id == user_roles.c.role_id)
                    .join(
                        role_permissions,
                        role_permissions.c.role_id == Role.id,
                    )
                    .where(
                        role_permissions.c.permission_id.in_(permission_ids),
                        Role.is_enabled.is_(True),
                    )
                    .distinct()
                ).all()
            )
        if tuple(admin_grants_added):
            user_ids.update(
                self.db.scalars(
                    select(user_roles.c.user_id)
                    .where(user_roles.c.role_id == admin_role_id)
                    .distinct()
                ).all()
            )
        return user_ids

    def _validate_scan_result(self, scan_result: PermissionScanResult) -> None:
        declared_names = set(scan_result.permission_names)
        binding_names = {binding.permission_name for binding in scan_result.bindings}
        if not binding_names <= declared_names:
            unknown = sorted(binding_names - declared_names)
            raise PermissionSyncError(
                f"scan result contains bindings for undeclared permissions: {unknown}"
            )

    def _now(self) -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)


def _bindings_by_permission(
    bindings: Iterable[ScannedPermissionBinding],
) -> dict[str, set[ScannedPermissionBinding]]:
    grouped: dict[str, set[ScannedPermissionBinding]] = {}
    for binding in bindings:
        grouped.setdefault(binding.permission_name, set()).add(binding)
    return grouped


def _group_binding_rows(rows: Iterable[tuple[str, str, str, str]]) -> dict[str, list[tuple[str, str, str]]]:
    grouped: dict[str, list[tuple[str, str, str]]] = {}
    for name, http_method, path, route_name in rows:
        grouped.setdefault(name, []).append((http_method, path, route_name))
    return grouped


def _binding_to_dict(binding: ScannedPermissionBinding) -> dict[str, str]:
    return {
        "permission_name": binding.permission_name,
        "http_method": binding.http_method,
        "path": binding.path,
        "route_name": binding.route_name,
    }
