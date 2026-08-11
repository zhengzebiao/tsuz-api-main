from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.core.logging import request_id_context
from app.core.security import hash_password
from app.models.audit_event import AuditEvent
from app.models.role import Role, user_roles
from app.models.user import User
from app.schemas.admin_user import AdminUserCreate, AdminUserUpdate
from app.services.session_service import SessionService


class AdminUserError(ValueError):
    code = "ADMIN_USER_ERROR"


class UserNotFoundError(AdminUserError):
    code = "USER_NOT_FOUND"


class EmailAlreadyExistsError(AdminUserError):
    code = "EMAIL_ALREADY_EXISTS"


class UserVersionConflictError(AdminUserError):
    code = "USER_VERSION_CONFLICT"


class InvalidPasswordError(AdminUserError):
    code = "INVALID_PASSWORD"


class UserBlacklistedError(AdminUserError):
    code = "USER_BLACKLISTED"


class SelfOperationNotAllowedError(AdminUserError):
    code = "SELF_OPERATION_NOT_ALLOWED"


class LastActiveAdminError(AdminUserError):
    code = "LAST_ACTIVE_ADMIN"


class AdminUserService:
    ADMIN_ROLE_NAME = "admin"
    PASSWORD_MIN_LENGTH = 10
    PASSWORD_MAX_LENGTH = 128

    def __init__(self, db: DbSession) -> None:
        self.db = db
        self.sessions = SessionService(db)

    def list_users(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        keyword: str | None = None,
        is_active: bool | None = None,
        is_blacklisted: bool | None = None,
    ) -> tuple[list[User], int]:
        query = select(User)
        count_query = select(func.count()).select_from(User)
        filters: list[Any] = []
        if keyword:
            normalized_keyword = keyword.strip().lower()
            if normalized_keyword:
                keyword_filter = or_(
                    func.lower(User.email).contains(normalized_keyword),
                    func.lower(User.display_name).contains(normalized_keyword),
                )
                filters.append(keyword_filter)
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

    def get_user(self, user_id: int) -> User:
        user = self.db.get(User, user_id)
        if user is None:
            raise UserNotFoundError(UserNotFoundError.code)
        return user

    def create_user(self, payload: AdminUserCreate, *, actor_user_id: int, request_id: str | None = None) -> User:
        email = self._normalize_email(payload.email)
        self._validate_password(payload.password)
        if self.db.scalar(select(User.id).where(User.email == email)) is not None:
            raise EmailAlreadyExistsError(EmailAlreadyExistsError.code)
        user = User(
            email=email,
            display_name=payload.display_name,
            hashed_password=hash_password(payload.password),
            is_active=payload.is_active,
            is_blacklisted=False,
        )
        self.db.add(user)
        try:
            self.db.flush()
        except IntegrityError as exc:
            self.db.rollback()
            raise EmailAlreadyExistsError(EmailAlreadyExistsError.code) from exc
        self._add_audit(
            actor_user_id=actor_user_id,
            action="user.created",
            target_id=user.id,
            result="success",
            request_id=request_id,
            changes={
                "email": {"from": None, "to": user.email},
                "display_name": {"from": None, "to": user.display_name},
                "is_active": {"from": None, "to": user.is_active},
            },
        )
        self.db.commit()
        self.db.refresh(user)
        return user

    def update_user(
        self,
        user_id: int,
        payload: AdminUserUpdate,
        *,
        actor_user_id: int,
        request_id: str | None = None,
    ) -> tuple[User, bool, int]:
        current = self.get_user(user_id)
        changes = payload.model_dump(exclude_unset=True, exclude={"version"})
        if "email" in changes:
            changes["email"] = self._normalize_email(changes["email"])
        changes = {key: value for key, value in changes.items() if getattr(current, key) != value}
        before = {key: getattr(current, key) for key in changes}
        if not changes:
            self._add_audit(
                actor_user_id=actor_user_id,
                action="user.updated",
                target_id=user_id,
                result="no_change",
                request_id=request_id,
                changes={},
            )
            self.db.commit()
            return current, False, 0

        values = {**changes, "version": User.version + 1, "updated_at": self._now()}
        try:
            result = self.db.execute(
                update(User).where(User.id == user_id, User.version == payload.version).values(**values)
            )
            if result.rowcount != 1:
                self.db.rollback()
                if self.db.get(User, user_id) is None:
                    raise UserNotFoundError(UserNotFoundError.code)
                raise UserVersionConflictError(UserVersionConflictError.code)
            revoked_sessions = 0
            if "email" in changes:
                revoked_sessions = self.sessions.revoke_user_sessions(user_id, "email_changed")
            self._add_audit(
                actor_user_id=actor_user_id,
                action="user.updated",
                target_id=user_id,
                result="success",
                request_id=request_id,
                changes={key: {"from": before[key], "to": value} for key, value in changes.items()},
            )
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise EmailAlreadyExistsError(EmailAlreadyExistsError.code) from exc
        user = self.get_user(user_id)
        return user, True, revoked_sessions

    def disable_user(
        self,
        user_id: int,
        *,
        actor_user_id: int,
        reason: str,
        request_id: str | None = None,
    ) -> tuple[User, bool, int]:
        self._validate_reason(reason)
        user = self._lock_user(user_id)
        self._ensure_not_self(user_id, actor_user_id)
        if not user.is_active:
            return self._finish_no_change(user, "user.disabled", actor_user_id, reason, request_id)
        self._ensure_can_remove_admin(user)
        before = {"is_active": user.is_active, "disabled_at": user.disabled_at, "disabled_reason": user.disabled_reason}
        user.is_active = False
        user.disabled_at = self._now()
        user.disabled_reason = reason
        self._increment_version(user)
        revoked_sessions = self.sessions.revoke_user_sessions(user.id, "user_disabled")
        return self._finish_change(user, "user.disabled", actor_user_id, reason, request_id, before, revoked_sessions)

    def enable_user(
        self,
        user_id: int,
        *,
        actor_user_id: int,
        request_id: str | None = None,
    ) -> tuple[User, bool, int]:
        user = self._lock_user(user_id)
        if user.is_blacklisted:
            self._abort(UserBlacklistedError(UserBlacklistedError.code))
        if user.is_active:
            return self._finish_no_change(user, "user.enabled", actor_user_id, None, request_id)
        before = {"is_active": user.is_active, "disabled_at": user.disabled_at, "disabled_reason": user.disabled_reason}
        user.is_active = True
        user.disabled_at = None
        user.disabled_reason = None
        self._increment_version(user)
        return self._finish_change(user, "user.enabled", actor_user_id, None, request_id, before, 0)

    def blacklist_user(
        self,
        user_id: int,
        *,
        actor_user_id: int,
        reason: str,
        request_id: str | None = None,
    ) -> tuple[User, bool, int]:
        self._validate_reason(reason)
        user = self._lock_user(user_id)
        self._ensure_not_self(user_id, actor_user_id)
        if user.is_blacklisted:
            return self._finish_no_change(user, "user.blacklisted", actor_user_id, reason, request_id)
        self._ensure_can_remove_admin(user)
        before = {"is_blacklisted": user.is_blacklisted, "blacklisted_at": user.blacklisted_at, "blacklisted_reason": user.blacklisted_reason}
        user.is_blacklisted = True
        user.blacklisted_at = self._now()
        user.blacklisted_reason = reason
        self._increment_version(user)
        revoked_sessions = self.sessions.revoke_user_sessions(user.id, "user_blacklisted")
        return self._finish_change(user, "user.blacklisted", actor_user_id, reason, request_id, before, revoked_sessions)

    def recover_user(
        self,
        user_id: int,
        *,
        actor_user_id: int,
        request_id: str | None = None,
    ) -> tuple[User, bool, int]:
        user = self._lock_user(user_id)
        if not user.is_blacklisted:
            return self._finish_no_change(user, "user.recovered", actor_user_id, None, request_id)
        before = {
            "is_blacklisted": user.is_blacklisted,
            "blacklisted_at": user.blacklisted_at,
            "blacklisted_reason": user.blacklisted_reason,
        }
        user.is_blacklisted = False
        user.blacklisted_at = None
        user.blacklisted_reason = None
        self._increment_version(user)
        return self._finish_change(user, "user.recovered", actor_user_id, None, request_id, before, 0)

    def reset_password(
        self,
        user_id: int,
        new_password: str,
        *,
        actor_user_id: int,
        request_id: str | None = None,
    ) -> int:
        self._validate_password(new_password)
        password_hash = hash_password(new_password)
        user = self._lock_user(user_id)
        user.hashed_password = password_hash
        user.password_changed_at = self._now()
        self._increment_version(user)
        revoked_sessions = self.sessions.revoke_user_sessions(user.id, "password_reset")
        self._add_audit(
            actor_user_id=actor_user_id,
            action="user.password_reset",
            target_id=user.id,
            result="success",
            request_id=request_id,
            changes={"password_changed": True, "revoked_sessions": revoked_sessions},
        )
        self.db.commit()
        return revoked_sessions

    def force_logout(
        self,
        user_id: int,
        *,
        actor_user_id: int,
        reason: str | None = None,
        request_id: str | None = None,
    ) -> int:
        user = self._lock_user(user_id)
        revoked_sessions = self.sessions.revoke_user_sessions(user.id, "admin_force_logout")
        self._add_audit(
            actor_user_id=actor_user_id,
            action="user.force_logout",
            target_id=user.id,
            result="success",
            reason=reason,
            request_id=request_id,
            changes={"revoked_sessions": revoked_sessions},
        )
        self.db.commit()
        return revoked_sessions

    def _lock_user(self, user_id: int) -> User:
        user = self.db.scalar(select(User).where(User.id == user_id).with_for_update())
        if user is None:
            self._abort(UserNotFoundError(UserNotFoundError.code))
        return user

    def _ensure_not_self(self, user_id: int, actor_user_id: int) -> None:
        if user_id == actor_user_id:
            self._abort(SelfOperationNotAllowedError(SelfOperationNotAllowedError.code))

    def _ensure_can_remove_admin(self, user: User) -> None:
        if not self._is_admin(user.id) or not user.is_active or user.is_blacklisted:
            return
        role = self.db.scalar(select(Role).where(Role.name == self.ADMIN_ROLE_NAME).with_for_update())
        if role is None:
            return
        active_admin_count = self.db.scalar(
            select(func.count(func.distinct(User.id)))
            .select_from(User)
            .join(user_roles, user_roles.c.user_id == User.id)
            .where(
                user_roles.c.role_id == role.id,
                User.is_active.is_(True),
                User.is_blacklisted.is_(False),
            )
        )
        if (active_admin_count or 0) <= 1:
            self._abort(LastActiveAdminError(LastActiveAdminError.code))

    def _is_admin(self, user_id: int) -> bool:
        return (
            self.db.scalar(
                select(user_roles.c.user_id).join(Role, Role.id == user_roles.c.role_id).where(
                    user_roles.c.user_id == user_id,
                    Role.name == self.ADMIN_ROLE_NAME,
                )
            )
            is not None
        )

    def _finish_no_change(
        self,
        user: User,
        action: str,
        actor_user_id: int,
        reason: str | None,
        request_id: str | None,
    ) -> tuple[User, bool, int]:
        self._add_audit(
            actor_user_id=actor_user_id,
            action=action,
            target_id=user.id,
            result="no_change",
            reason=reason,
            request_id=request_id,
            changes={},
        )
        self.db.commit()
        return user, False, 0

    def _finish_change(
        self,
        user: User,
        action: str,
        actor_user_id: int,
        reason: str | None,
        request_id: str | None,
        before: dict[str, Any],
        revoked_sessions: int,
    ) -> tuple[User, bool, int]:
        self._add_audit(
            actor_user_id=actor_user_id,
            action=action,
            target_id=user.id,
            result="success",
            reason=reason,
            request_id=request_id,
            changes={key: {"from": value, "to": getattr(user, key)} for key, value in before.items()},
        )
        self.db.commit()
        return user, True, revoked_sessions

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
                target_type="user",
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

    def _validate_password(self, password: str) -> None:
        if not self.PASSWORD_MIN_LENGTH <= len(password) <= self.PASSWORD_MAX_LENGTH:
            raise InvalidPasswordError(InvalidPasswordError.code)

    def _validate_reason(self, reason: str) -> None:
        normalized = reason.strip()
        if not normalized or len(normalized) > 500:
            raise ValueError("invalid reason")

    def _normalize_email(self, email: str) -> str:
        return str(email).strip().lower()

    def _increment_version(self, user: User) -> None:
        user.version += 1
        user.updated_at = self._now()

    def _now(self) -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def _abort(self, error: AdminUserError) -> None:
        self.db.rollback()
        raise error
