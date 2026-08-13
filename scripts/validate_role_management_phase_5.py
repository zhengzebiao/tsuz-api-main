from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from redis import Redis
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session as DbSession, sessionmaker
from sqlalchemy.pool import NullPool

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

DEFAULT_ADMIN_DATABASE_URL = "postgresql+psycopg://test_user:test_password@127.0.0.1:55432/postgres"
DEFAULT_REDIS_URL = "redis://127.0.0.1:56379/15"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
HEAD_REVISION = "0005_permission_management"
ROLE_PERMISSIONS = (
    "role:read",
    "role:create",
    "role:update",
    "role:disable",
    "role:enable",
    "user:assign_roles",
)
SENSITIVE_RESPONSE_FIELDS = {
    "access_token",
    "refresh_token",
    "password",
    "new_password",
    "hashed_password",
    "sid",
}


class RolePhase5ValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class RolePhase5Config:
    admin_database_url: str
    redis_url: str
    api_host: str
    api_port: int
    allow_remote: bool
    allow_default_ports: bool

    @classmethod
    def from_env(cls) -> RolePhase5Config:
        config = cls(
            admin_database_url=os.getenv("ROLE_PHASE5_ADMIN_DATABASE_URL", DEFAULT_ADMIN_DATABASE_URL),
            redis_url=os.getenv("ROLE_PHASE5_REDIS_URL", DEFAULT_REDIS_URL),
            api_host=os.getenv("ROLE_PHASE5_API_HOST", "127.0.0.1"),
            api_port=int(os.getenv("ROLE_PHASE5_API_PORT", "0")),
            allow_remote=os.getenv("ROLE_PHASE5_ALLOW_REMOTE", "0") == "1",
            allow_default_ports=os.getenv("ROLE_PHASE5_ALLOW_DEFAULT_PORTS", "0") == "1",
        )
        config.validate()
        return config

    def validate(self) -> None:
        database_url = make_url(self.admin_database_url)
        redis_url = urlparse(self.redis_url)
        if not database_url.drivername.startswith("postgresql"):
            raise RolePhase5ValidationError("ROLE_PHASE5_ADMIN_DATABASE_URL must use PostgreSQL")
        if redis_url.scheme not in {"redis", "rediss"}:
            raise RolePhase5ValidationError("ROLE_PHASE5_REDIS_URL must use redis:// or rediss://")
        if not 0 <= self.api_port <= 65535:
            raise RolePhase5ValidationError("ROLE_PHASE5_API_PORT must be between 0 and 65535")
        if not self.allow_remote:
            hosts = {database_url.host, redis_url.hostname, self.api_host}
            remote_hosts = sorted(host for host in hosts if host and host not in LOCAL_HOSTS)
            if remote_hosts:
                raise RolePhase5ValidationError(
                    "role phase 5 validation only allows local services by default; "
                    "set ROLE_PHASE5_ALLOW_REMOTE=1 for an explicitly approved isolated environment"
                )
        if not self.allow_default_ports:
            database_port = database_url.port or 5432
            redis_port = redis_url.port or 6379
            if database_port == 5432 or redis_port == 6379:
                raise RolePhase5ValidationError(
                    "role phase 5 validation refuses PostgreSQL 5432 or Redis 6379 by default; "
                    "use dedicated temporary ports or explicitly set ROLE_PHASE5_ALLOW_DEFAULT_PORTS=1"
                )


@dataclass(frozen=True)
class TemporaryDatabase:
    name: str
    url: str


T = TypeVar("T")


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise RolePhase5ValidationError(message)


def _redact(value: str, secrets: Sequence[str] = ()) -> str:
    redacted = value
    for secret in sorted((secret for secret in secrets if secret), key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _database_secrets(database_url: str) -> list[str]:
    url = make_url(database_url)
    return [url.password or "", database_url]


def _run_command(command: Sequence[str], *, env: dict[str, str], secrets: Sequence[str] = ()) -> str:
    completed = subprocess.run(
        list(command),
        cwd=ROOT_DIR,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    if completed.returncode != 0:
        safe_output = _redact(output, secrets)
        raise RolePhase5ValidationError(f"command failed ({' '.join(command)}):\n{safe_output}")
    return output


def _alembic(database_url: str, *arguments: str) -> str:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    return _run_command(
        (sys.executable, "-m", "alembic", *arguments),
        env=env,
        secrets=_database_secrets(database_url),
    )


def _database_url_for(admin_database_url: str, database_name: str) -> str:
    return make_url(admin_database_url).set(database=database_name).render_as_string(hide_password=False)


def _quoted_identifier(identifier: str) -> str:
    if not identifier.replace("_", "").isalnum():
        raise RolePhase5ValidationError("generated database name contains invalid characters")
    return f'"{identifier}"'


@contextmanager
def temporary_postgres_database(config: RolePhase5Config, purpose: str) -> Iterator[TemporaryDatabase]:
    suffix = uuid4().hex[:12]
    database_name = f"tsuz_role_phase5_{purpose}_{suffix}"
    admin_engine = create_engine(config.admin_database_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    created = False
    try:
        with admin_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            connection.exec_driver_sql(f"CREATE DATABASE {_quoted_identifier(database_name)}")
            created = True
        yield TemporaryDatabase(database_name, _database_url_for(config.admin_database_url, database_name))
    finally:
        try:
            if created:
                with admin_engine.connect() as connection:
                    connection.execute(
                        text(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                        ),
                        {"database_name": database_name},
                    )
                    connection.exec_driver_sql(f"DROP DATABASE IF EXISTS {_quoted_identifier(database_name)}")
        finally:
            admin_engine.dispose()


def _generate_jwt_keys() -> tuple[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem.decode(), public_pem.decode()


def _runtime_env(database_url: str, redis_url: str, redis_prefix: str) -> dict[str, str]:
    private_key, public_key = _generate_jwt_keys()
    env = os.environ.copy()
    env.update(
        {
            "APP_ENV": "role-phase5",
            "DEBUG": "false",
            "LOG_LEVEL": "info",
            "LOG_FORMAT": "json",
            "DATABASE_URL": database_url,
            "REDIS_URL": redis_url,
            "REDIS_KEY_PREFIX": redis_prefix,
            "JWT_ALGORITHM": "RS256",
            "JWT_ISSUER": "auth-service-role-phase5",
            "JWT_AUDIENCE": "backend-api-role-phase5",
            "JWT_PRIVATE_KEY": private_key,
            "JWT_PUBLIC_KEY": public_key,
            "ACCESS_TOKEN_EXPIRE_MINUTES": "10",
            "REFRESH_TOKEN_EXPIRE_DAYS": "1",
            "REFRESH_TOKEN_REUSE_GRACE_SECONDS": "0",
            "TOKEN_BLACKLIST_PREFIX": f"{redis_prefix}blacklist:jti:",
            "REFRESH_TOKEN_PREFIX": f"{redis_prefix}refresh:",
            "SESSION_PREFIX": f"{redis_prefix}session:",
            "CORS_ALLOW_ORIGINS": "http://127.0.0.1",
            "OPENAPI_ENABLED": "true",
            "DOCS_ENABLED": "false",
            "REDOC_ENABLED": "false",
            "WEB_CONCURRENCY": "1",
        }
    )
    return env


def _seed(env: dict[str, str]) -> None:
    _run_command(
        (sys.executable, "-m", "app.seed"),
        env=env,
        secrets=_database_secrets(env["DATABASE_URL"]),
    )


def _sync_permissions(env: dict[str, str]) -> None:
    _run_command(
        (sys.executable, "-m", "app.commands.sync_permissions"),
        env=env,
        secrets=_database_secrets(env["DATABASE_URL"]),
    )


def _clean_redis_namespace(redis_client: Redis, prefix: str) -> int:
    deleted = 0
    batch: list[str] = []
    for key in redis_client.scan_iter(match=f"{prefix}*"):
        batch.append(str(key))
        if len(batch) >= 100:
            deleted += int(redis_client.delete(*batch))
            batch.clear()
    if batch:
        deleted += int(redis_client.delete(*batch))
    return deleted


def run_migration_validation(config: RolePhase5Config) -> dict[str, Any]:
    with temporary_postgres_database(config, "migration") as database:
        _alembic(database.url, "upgrade", "0003_app_management")
        engine = create_engine(database.url, poolclass=NullPool)
        legacy_email = f"legacy-role-{uuid4().hex[:8]}@example.com"
        legacy_role_name = f"legacy_role_{uuid4().hex[:8]}"
        legacy_permission_name = f"legacy:role:{uuid4().hex[:8]}"
        try:
            with engine.begin() as connection:
                user_id = connection.execute(
                    text(
                        "INSERT INTO users "
                        "(email, hashed_password, is_active, is_blacklisted, version) "
                        "VALUES (:email, 'legacy-hash', true, false, 1) RETURNING id"
                    ),
                    {"email": legacy_email},
                ).scalar_one()
                role_id = connection.execute(
                    text("INSERT INTO roles (name) VALUES (:name) RETURNING id"),
                    {"name": legacy_role_name},
                ).scalar_one()
                permission_id = connection.execute(
                    text(
                        "INSERT INTO permissions (name, description) "
                        "VALUES (:name, 'Legacy role permission') RETURNING id"
                    ),
                    {"name": legacy_permission_name},
                ).scalar_one()
                connection.execute(
                    text("INSERT INTO user_roles (user_id, role_id) VALUES (:user_id, :role_id)"),
                    {"user_id": user_id, "role_id": role_id},
                )
                connection.execute(
                    text(
                        "INSERT INTO role_permissions (role_id, permission_id) "
                        "VALUES (:role_id, :permission_id)"
                    ),
                    {"role_id": role_id, "permission_id": permission_id},
                )
        finally:
            engine.dispose()

        _alembic(database.url, "upgrade", "head")
        engine = create_engine(database.url, poolclass=NullPool)
        expected_columns = {
            "id",
            "name",
            "description",
            "is_enabled",
            "disabled_at",
            "disabled_reason",
            "created_at",
            "updated_at",
            "version",
        }
        try:
            inspector = inspect(engine)
            columns = {column["name"]: column for column in inspector.get_columns("roles")}
            indexes = {index["name"]: index for index in inspector.get_indexes("roles")}
            _assert(set(columns) == expected_columns, "roles table columns do not match the model")
            for column_name in {"id", "name", "description", "is_enabled", "created_at", "updated_at", "version"}:
                _assert(columns[column_name]["nullable"] is False, f"{column_name} must be NOT NULL")
            for column_name in {"disabled_at", "disabled_reason"}:
                _assert(columns[column_name]["nullable"] is True, f"{column_name} must be nullable")
            _assert({"ix_roles_id", "ix_roles_name", "ix_roles_is_enabled"} <= set(indexes), "roles indexes are incomplete")
            _assert(indexes["ix_roles_name"]["unique"] is True, "role name index is not unique")
            _assert(indexes["ix_roles_is_enabled"]["unique"] is False, "role state index must not be unique")
            with engine.begin() as connection:
                legacy = connection.execute(
                    text(
                        "SELECT description, is_enabled, disabled_at, disabled_reason, "
                        "created_at, updated_at, version FROM roles WHERE name = :name"
                    ),
                    {"name": legacy_role_name},
                ).mappings().one()
                associations = connection.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM user_roles ur JOIN users u ON u.id = ur.user_id "
                        "JOIN roles r ON r.id = ur.role_id WHERE u.email = :email AND r.name = :role_name) "
                        "+ (SELECT count(*) FROM role_permissions rp "
                        "JOIN roles r ON r.id = rp.role_id "
                        "JOIN permissions p ON p.id = rp.permission_id "
                        "WHERE r.name = :role_name AND p.name = :permission_name)"
                    ),
                    {
                        "email": legacy_email,
                        "role_name": legacy_role_name,
                        "permission_name": legacy_permission_name,
                    },
                ).scalar_one()
                inserted = connection.execute(
                    text(
                        "INSERT INTO roles (name) VALUES (:name) "
                        "RETURNING description, is_enabled, disabled_at, disabled_reason, "
                        "created_at, updated_at, version"
                    ),
                    {"name": f"role_defaults_{uuid4().hex[:8]}"},
                ).mappings().one()
            for values in (legacy, inserted):
                _assert(values["description"] == "", "role description default is not an empty string")
                _assert(values["is_enabled"] is True, "role enabled default is not true")
                _assert(values["disabled_at"] is None, "role disabled_at default is not null")
                _assert(values["disabled_reason"] is None, "role disabled_reason default is not null")
                _assert(values["version"] == 1, "role version default is not 1")
                _assert(values["created_at"] is not None and values["updated_at"] is not None, "role timestamps are missing")
            _assert(associations == 2, "legacy role associations were not preserved after upgrade")
        finally:
            engine.dispose()

        check_output = _alembic(database.url, "check")
        _assert("No new upgrade operations detected" in check_output, "Alembic metadata check is not clean")
        _alembic(database.url, "downgrade", "0003_app_management")
        engine = create_engine(database.url, poolclass=NullPool)
        try:
            inspector = inspect(engine)
            downgraded_columns = {column["name"] for column in inspector.get_columns("roles")}
            downgraded_indexes = {index["name"] for index in inspector.get_indexes("roles")}
            _assert(downgraded_columns == {"id", "name"}, "role management columns survived downgrade")
            _assert("ix_roles_is_enabled" not in downgraded_indexes, "role state index survived downgrade")
            with engine.connect() as connection:
                preserved = connection.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM users WHERE email = :email) "
                        "+ (SELECT count(*) FROM roles WHERE name = :role_name) "
                        "+ (SELECT count(*) FROM permissions WHERE name = :permission_name) "
                        "+ (SELECT count(*) FROM user_roles ur JOIN users u ON u.id = ur.user_id "
                        "JOIN roles r ON r.id = ur.role_id WHERE u.email = :email AND r.name = :role_name) "
                        "+ (SELECT count(*) FROM role_permissions rp "
                        "JOIN roles r ON r.id = rp.role_id "
                        "JOIN permissions p ON p.id = rp.permission_id "
                        "WHERE r.name = :role_name AND p.name = :permission_name)"
                    ),
                    {
                        "email": legacy_email,
                        "role_name": legacy_role_name,
                        "permission_name": legacy_permission_name,
                    },
                ).scalar_one()
            _assert(preserved == 5, "legacy role data was lost during downgrade")
        finally:
            engine.dispose()

        _alembic(database.url, "upgrade", "head")
        current_output = _alembic(database.url, "current")
        _assert(HEAD_REVISION in current_output, "database did not return to the role migration head")
        return {
            "database": database.name,
            "current_revision": HEAD_REVISION,
            "alembic_check": "clean",
            "role_columns_verified": len(expected_columns),
            "role_indexes_verified": 3,
            "legacy_role_data_preserved": True,
            "role_associations_preserved": True,
            "temporary_resources_cleaned": True,
        }


def _wait_until_lock_wait(engine: Engine, backend_pid: int, timeout_seconds: float = 5) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            waiting = connection.execute(
                text(
                    "SELECT wait_event_type = 'Lock' FROM pg_stat_activity "
                    "WHERE pid = :backend_pid"
                ),
                {"backend_pid": backend_pid},
            ).scalar_one_or_none()
        if waiting is True:
            return True
        time.sleep(0.05)
    return False


def _run_locked_pair(
    engine: Engine,
    first: Callable[[DbSession], T],
    second: Callable[[DbSession], T],
) -> tuple[T, T, bool]:
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    first_locked = threading.Event()
    release_first = threading.Event()
    second_pid_ready = threading.Event()
    second_pid: list[int] = []

    def first_task() -> T:
        with SessionLocal() as db:
            result = first(db)
            first_locked.set()
            _assert(release_first.wait(timeout=10), "timed out waiting to release the first transaction")
            db.commit()
            return result

    def second_task() -> T:
        _assert(first_locked.wait(timeout=10), "first transaction did not acquire its lock")
        with SessionLocal() as db:
            second_pid.append(int(db.scalar(text("SELECT pg_backend_pid()"))))
            second_pid_ready.set()
            result = second(db)
            db.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first_task)
        second_future = executor.submit(second_task)
        _assert(second_pid_ready.wait(timeout=10), "second transaction did not start")
        lock_wait_verified = _wait_until_lock_wait(engine, second_pid[0])
        release_first.set()
        return first_future.result(timeout=10), second_future.result(timeout=10), lock_wait_verified


def run_concurrency_validation(config: RolePhase5Config) -> dict[str, Any]:
    with temporary_postgres_database(config, "concurrency") as database:
        _alembic(database.url, "upgrade", "head")
        engine = create_engine(database.url, poolclass=NullPool)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        redis_prefix = f"auth:role-phase5:concurrency:{uuid4().hex[:12]}:"
        redis_client: Redis = Redis.from_url(config.redis_url, decode_responses=True)
        redis_client.ping()
        _clean_redis_namespace(redis_client, redis_prefix)
        from app.core.config import settings
        from app.core.security import hash_password
        from app.models.audit_event import AuditEvent
        from app.models.permission import Permission
        from app.models.role import Role, role_permissions, user_roles
        from app.models.user import User
        from app.schemas.admin_role import AdminRoleUpdate
        from app.services.admin_role_service import AdminRoleService, RoleVersionConflictError
        from app.services.admin_user_service import AdminUserService, UserVersionConflictError

        original_session_prefix = settings.session_prefix
        original_redis_url = settings.redis_url
        try:
            settings.redis_url = config.redis_url
            settings.session_prefix = redis_prefix
            with SessionLocal() as db:
                actor = User(
                    email=f"role-concurrency-actor-{uuid4().hex[:8]}@example.com",
                    hashed_password=hash_password("role-concurrency-password"),
                    is_active=True,
                    is_blacklisted=False,
                )
                target = User(
                    email=f"role-concurrency-target-{uuid4().hex[:8]}@example.com",
                    hashed_password=hash_password("role-concurrency-password"),
                    is_active=True,
                    is_blacklisted=False,
                )
                role = Role(name=f"concurrent_role_{uuid4().hex[:8]}", description="Concurrent role")
                old_role = Role(name=f"old_role_{uuid4().hex[:8]}")
                first_target_role = Role(name=f"first_target_{uuid4().hex[:8]}")
                second_target_role = Role(name=f"second_target_{uuid4().hex[:8]}")
                permission = Permission(name=f"concurrent:permission:{uuid4().hex[:8]}", description="Concurrency")
                db.add_all((actor, target, role, old_role, first_target_role, second_target_role, permission))
                db.flush()
                actor_id = actor.id
                target_id = target.id
                role_id = role.id
                old_role_id = old_role.id
                first_target_role_id = first_target_role.id
                second_target_role_id = second_target_role.id
                db.execute(user_roles.insert().values(user_id=target_id, role_id=role_id))
                db.execute(user_roles.insert().values(user_id=target_id, role_id=old_role_id))
                db.execute(role_permissions.insert().values(role_id=role_id, permission_id=permission.id))
                db.commit()

            disable_first, disable_second, disable_waited = _run_locked_pair(
                engine,
                lambda db: AdminRoleService(db).disable_role(
                    role_id,
                    actor_user_id=actor_id,
                    reason="first concurrency reason",
                    request_id="role-phase5-disable-first",
                ),
                lambda db: AdminRoleService(db).disable_role(
                    role_id,
                    actor_user_id=actor_id,
                    reason="second concurrency reason",
                    request_id="role-phase5-disable-second",
                ),
            )
            _assert(disable_waited, "concurrent role disable did not wait on a PostgreSQL row lock")
            _assert(disable_first[1] is True and disable_second[1] is False, "role disable idempotency failed")
            with SessionLocal() as db:
                disabled = db.get(Role, role_id)
                _assert(disabled is not None and disabled.version == 2, "concurrent disable produced the wrong role version")
                _assert(disabled.disabled_reason == "first concurrency reason", "concurrent disable replaced the first reason")
                disabled_audits = db.scalars(
                    select(AuditEvent).where(
                        AuditEvent.target_type == "role",
                        AuditEvent.target_id == role_id,
                        AuditEvent.action == "role.disabled",
                    )
                ).all()
                _assert(len(disabled_audits) == 1, "concurrent disable created duplicate audits")
                _assert(
                    db.scalar(select(func.count()).select_from(user_roles).where(user_roles.c.role_id == role_id)) == 1,
                    "role disable removed a user association",
                )
                _assert(
                    db.scalar(
                        select(func.count()).select_from(role_permissions).where(role_permissions.c.role_id == role_id)
                    )
                    == 1,
                    "role disable removed a permission association",
                )

            enable_first, enable_second, enable_waited = _run_locked_pair(
                engine,
                lambda db: AdminRoleService(db).enable_role(
                    role_id,
                    actor_user_id=actor_id,
                    request_id="role-phase5-enable-first",
                ),
                lambda db: AdminRoleService(db).enable_role(
                    role_id,
                    actor_user_id=actor_id,
                    request_id="role-phase5-enable-second",
                ),
            )
            _assert(enable_waited, "concurrent role enable did not wait on a PostgreSQL row lock")
            _assert(enable_first[1] is True and enable_second[1] is False, "role enable idempotency failed")
            with SessionLocal() as db:
                enabled = db.get(Role, role_id)
                _assert(enabled is not None and enabled.version == 3, "concurrent enable produced the wrong role version")
                _assert(enabled.is_enabled is True, "concurrent enable left the role disabled")
                _assert(enabled.disabled_at is None and enabled.disabled_reason is None, "enable did not clear disable metadata")
                enabled_audits = db.scalars(
                    select(AuditEvent).where(
                        AuditEvent.target_type == "role",
                        AuditEvent.target_id == role_id,
                        AuditEvent.action == "role.enabled",
                    )
                ).all()
                _assert(len(enabled_audits) == 1, "concurrent enable created duplicate audits")

            role_assignment_conflict: list[str] = []

            def first_assignment(db: DbSession) -> tuple[Any, list[Any], bool, int]:
                return AdminUserService(db).assign_roles(
                    target_id,
                    [role_id, first_target_role_id],
                    1,
                    actor_user_id=actor_id,
                    request_id="role-phase5-assignment-first",
                )

            def second_assignment(db: DbSession) -> str:
                try:
                    AdminUserService(db).assign_roles(
                        target_id,
                        [role_id, second_target_role_id],
                        1,
                        actor_user_id=actor_id,
                        request_id="role-phase5-assignment-second",
                    )
                except UserVersionConflictError as exc:
                    role_assignment_conflict.append(exc.code)
                    return exc.code
                raise RolePhase5ValidationError("stale concurrent role assignment did not conflict")

            assignment_first, assignment_second, assignment_waited = _run_locked_pair(
                engine,
                first_assignment,
                second_assignment,
            )
            _assert(assignment_waited, "concurrent role assignment did not wait on the user row lock")
            _assert(assignment_first[2] is True, "first concurrent role assignment did not change the user")
            _assert(assignment_second == "USER_VERSION_CONFLICT", "concurrent role assignment returned the wrong error")
            _assert(role_assignment_conflict == ["USER_VERSION_CONFLICT"], "role assignment conflict was not captured")
            with SessionLocal() as db:
                assigned_ids = set(
                    db.scalars(select(user_roles.c.role_id).where(user_roles.c.user_id == target_id)).all()
                )
                _assert(
                    assigned_ids == {role_id, first_target_role_id},
                    "concurrent role assignment left an inconsistent association set",
                )
                target_user = db.get(User, target_id)
                _assert(target_user is not None and target_user.version == 2, "concurrent assignment changed user version incorrectly")
                assignment_audits = db.scalars(
                    select(AuditEvent).where(
                        AuditEvent.target_type == "user",
                        AuditEvent.target_id == target_id,
                        AuditEvent.action == "user.roles_assigned",
                    )
                ).all()
                _assert(len(assignment_audits) == 1, "concurrent assignment created an extra audit")

            with SessionLocal() as setup_db:
                editable = Role(name=f"optimistic_role_{uuid4().hex[:8]}", description="Original")
                setup_db.add(editable)
                setup_db.commit()
                editable_id = editable.id

            with SessionLocal() as winning_db, SessionLocal() as stale_db:
                current = winning_db.get(Role, editable_id)
                stale = stale_db.get(Role, editable_id)
                _assert(current is not None and stale is not None, "role missing before optimistic lock validation")
                version = current.version
                updated, changed, _revoked = AdminRoleService(winning_db).update_role(
                    editable_id,
                    AdminRoleUpdate(description="Optimistic winner", version=version),
                    actor_user_id=actor_id,
                    request_id="role-phase5-update-winner",
                )
                _assert(changed and updated.version == version + 1, "winning role update failed")
                winning_db.commit()
                conflict_verified = False
                try:
                    AdminRoleService(stale_db).update_role(
                        editable_id,
                        AdminRoleUpdate(description="Stale loser", version=version),
                        actor_user_id=actor_id,
                        request_id="role-phase5-update-loser",
                    )
                except RoleVersionConflictError:
                    stale_db.rollback()
                    conflict_verified = True
                _assert(conflict_verified, "stale role update did not conflict")

            with SessionLocal() as db:
                final_role = db.get(Role, editable_id)
                _assert(final_role is not None and final_role.description == "Optimistic winner", "stale role update overwrote data")

            redis_keys = list(redis_client.scan_iter(match=f"{redis_prefix}*"))
            return {
                "database": database.name,
                "row_lock_waits_verified": 3,
                "disable_changes": [disable_first[1], disable_second[1]],
                "enable_changes": [enable_first[1], enable_second[1]],
                "role_assignment_conflict": "USER_VERSION_CONFLICT",
                "role_update_conflict": "ROLE_VERSION_CONFLICT",
                "associations_consistent": True,
                "redis_revocation_keys": len(redis_keys),
                "temporary_resources_cleaned": True,
            }
        finally:
            settings.session_prefix = original_session_prefix
            settings.redis_url = original_redis_url
            _clean_redis_namespace(redis_client, redis_prefix)
            redis_client.close()
            engine.dispose()


def _find_available_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _wait_for_api(base_url: str, process: subprocess.Popen[str], timeout_seconds: float = 30) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RolePhase5ValidationError("role phase 5 API process exited before becoming healthy")
        try:
            if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RolePhase5ValidationError("timed out waiting for the role phase 5 API process")


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _safe_response_body(response: httpx.Response) -> Any:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(body, dict):
        return {key: "[REDACTED]" if key in SENSITIVE_RESPONSE_FIELDS else value for key, value in body.items()}
    return body


def _expect(response: httpx.Response, expected_status: int, context: str) -> dict[str, Any]:
    if response.status_code != expected_status:
        raise RolePhase5ValidationError(
            f"{context} returned {response.status_code}, expected {expected_status}: {_safe_response_body(response)}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise RolePhase5ValidationError(f"{context} did not return JSON") from exc
    _assert(isinstance(body, dict), f"{context} returned an unexpected response shape")
    return body


def _authorization(access_token: str, request_id: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {access_token}"}
    if request_id:
        headers["X-Request-ID"] = request_id
    return headers


def _login(
    client: httpx.Client,
    email: str,
    password: str,
    env: dict[str, str],
    *,
    expected_status: int = 200,
) -> dict[str, Any]:
    response = client.post("/auth/login", json={"username": email, "password": password})
    if expected_status != 200:
        return _expect(response, expected_status, f"login {email}")
    body = _expect(response, 200, f"login {email}")
    _assert(isinstance(body.get("access_token"), str), "login response is missing access_token")
    _assert(isinstance(body.get("refresh_token"), str), "login response is missing refresh_token")
    payload = jwt.decode(
        str(body["access_token"]),
        env["JWT_PUBLIC_KEY"],
        algorithms=[env["JWT_ALGORITHM"]],
        issuer=env["JWT_ISSUER"],
        audience=env["JWT_AUDIENCE"],
    )
    body["verified_claims"] = payload
    return body


def _assert_no_sensitive_fields(payload: Any, context: str) -> None:
    serialized = json.dumps(payload, default=str).lower()
    for field in ("hashed_password", "access_token", "refresh_token", "permissions", "session_id"):
        _assert(field not in serialized, f"{context} exposed {field}")


def _token_sid(login: dict[str, Any]) -> str:
    sid = login["verified_claims"].get("sid")
    _assert(isinstance(sid, str) and bool(sid), "issued access token is missing sid")
    return str(sid)


def _assert_session_revoked(
    engine: Engine,
    redis_client: Redis,
    session_prefix: str,
    sid: str,
    reason: str,
    expected_ttl: int,
) -> None:
    with engine.connect() as connection:
        session = connection.execute(
            text("SELECT status, revoked_at, revoked_reason FROM sessions WHERE sid = :sid"),
            {"sid": sid},
        ).mappings().one()
    _assert(session["status"] == "revoked", f"session {sid[:12]} was not revoked in PostgreSQL")
    _assert(session["revoked_at"] is not None, f"session {sid[:12]} has no revoked_at")
    _assert(session["revoked_reason"] == reason, f"session {sid[:12]} has the wrong revocation reason")
    key = f"{session_prefix}{sid}"
    _assert(redis_client.get(key) == "revoked", f"session {sid[:12]} was not revoked in Redis")
    ttl = int(redis_client.ttl(key))
    _assert(0 < ttl <= expected_ttl, f"session {sid[:12]} has an invalid Redis TTL")


def _assert_access_and_refresh_revoked(
    client: httpx.Client,
    access_token: str,
    refresh_token: str,
) -> None:
    _expect(client.get("/auth/me", headers=_authorization(access_token)), 401, "revoked access token check")
    _expect(client.post("/auth/refresh", json={"refresh_token": refresh_token}), 401, "revoked refresh token check")


def _create_http_fixtures(database_url: str) -> dict[str, Any]:
    from app.core.security import hash_password
    from app.models.permission import Permission
    from app.models.role import Role, role_permissions, user_roles
    from app.models.user import User

    engine = create_engine(database_url, poolclass=NullPool)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    password = "role-phase5-permission-password"
    fixtures: dict[str, Any] = {"password": password, "principals": {}}
    try:
        with SessionLocal() as db:
            unrelated_permission = Permission(
                name=f"role-phase5:unrelated:{uuid4().hex[:8]}",
                description="Unrelated role phase 5 permission",
            )
            unrelated_role = Role(name=f"role_phase5_none_{uuid4().hex[:8]}")
            unrelated_user = User(
                email=f"role-phase5-none-{uuid4().hex[:8]}@example.com",
                hashed_password=hash_password(password),
                is_active=True,
                is_blacklisted=False,
            )
            db.add_all((unrelated_permission, unrelated_role, unrelated_user))
            db.flush()
            db.execute(
                role_permissions.insert().values(
                    role_id=unrelated_role.id,
                    permission_id=unrelated_permission.id,
                )
            )
            db.execute(user_roles.insert().values(user_id=unrelated_user.id, role_id=unrelated_role.id))
            fixtures["principals"]["none"] = {
                "email": unrelated_user.email,
                "role": unrelated_role.name,
                "scope": unrelated_permission.name,
            }

            for permission_name in (*ROLE_PERMISSIONS, "user:read"):
                permission = db.scalar(select(Permission).where(Permission.name == permission_name))
                _assert(permission is not None, f"permission sync did not create {permission_name}")
                slug = permission_name.replace(":", "_")
                role = Role(name=f"role_phase5_{slug}_{uuid4().hex[:6]}")
                user = User(
                    email=f"role-phase5-{slug}-{uuid4().hex[:8]}@example.com",
                    hashed_password=hash_password(password),
                    is_active=True,
                    is_blacklisted=False,
                )
                db.add_all((role, user))
                db.flush()
                db.execute(role_permissions.insert().values(role_id=role.id, permission_id=permission.id))
                db.execute(user_roles.insert().values(user_id=user.id, role_id=role.id))
                fixtures["principals"][permission_name] = {
                    "email": user.email,
                    "role": role.name,
                    "scope": permission_name,
                }

            target_permission = Permission(
                name=f"target:enabled:{uuid4().hex[:8]}",
                description="Target enabled permission",
            )
            disabled_permission = Permission(
                name=f"target:disabled:{uuid4().hex[:8]}",
                description="Target disabled permission",
            )
            target_role = Role(name=f"target_enabled_{uuid4().hex[:8]}")
            disabled_role = Role(
                name=f"target_disabled_{uuid4().hex[:8]}",
                is_enabled=False,
                disabled_reason="pre-existing disabled role",
            )
            unassigned_disabled_role = Role(
                name=f"target_unassigned_disabled_{uuid4().hex[:8]}",
                is_enabled=False,
                disabled_reason="must not be newly assigned",
            )
            target_user = User(
                email=f"role-phase5-target-{uuid4().hex[:8]}@example.com",
                hashed_password=hash_password(password),
                is_active=True,
                is_blacklisted=False,
            )
            db.add_all(
                (
                    target_permission,
                    disabled_permission,
                    target_role,
                    disabled_role,
                    unassigned_disabled_role,
                    target_user,
                )
            )
            db.flush()
            db.execute(
                role_permissions.insert().values(role_id=target_role.id, permission_id=target_permission.id)
            )
            db.execute(
                role_permissions.insert().values(
                    role_id=disabled_role.id,
                    permission_id=disabled_permission.id,
                )
            )
            db.execute(user_roles.insert().values(user_id=target_user.id, role_id=target_role.id))
            db.execute(user_roles.insert().values(user_id=target_user.id, role_id=disabled_role.id))
            fixtures["target"] = {
                "id": target_user.id,
                "email": target_user.email,
                "enabled_role_id": target_role.id,
                "enabled_role_name": target_role.name,
                "enabled_permission": target_permission.name,
                "disabled_role_id": disabled_role.id,
                "disabled_role_name": disabled_role.name,
                "disabled_permission": disabled_permission.name,
                "unassigned_disabled_role_id": unassigned_disabled_role.id,
            }
            db.commit()
    finally:
        engine.dispose()
    return fixtures


def _run_http_role_flow(
    client: httpx.Client,
    engine: Engine,
    redis_client: Redis,
    env: dict[str, str],
    fixtures: dict[str, Any],
) -> tuple[dict[str, Any], set[str]]:
    password = str(fixtures["password"])
    principals = fixtures["principals"]
    logins: dict[str, dict[str, Any]] = {}
    sensitive_values: set[str] = {password}
    for name, principal in principals.items():
        login = _login(client, principal["email"], password, env)
        claims = login["verified_claims"]
        _assert(claims["roles"] == [principal["role"]], f"{name} token has unexpected roles")
        _assert(set(str(claims["scope"]).split()) == {principal["scope"]}, f"{name} token has unexpected scope")
        logins[name] = login
        sensitive_values.update((str(login["access_token"]), str(login["refresh_token"]), _token_sid(login)))

    admin_login = _login(client, "admin@example.com", "password123", env)
    admin_claims = admin_login["verified_claims"]
    _assert(set(ROLE_PERMISSIONS) <= set(str(admin_claims["scope"]).split()), "admin token is missing role permissions")
    sensitive_values.update(
        (
            str(admin_login["access_token"]),
            str(admin_login["refresh_token"]),
            _token_sid(admin_login),
            "password123",
        )
    )

    target = fixtures["target"]
    target_login = _login(client, target["email"], password, env)
    target_claims = target_login["verified_claims"]
    _assert(target_claims["roles"] == [target["enabled_role_name"]], "disabled target role entered JWT roles")
    _assert(
        set(str(target_claims["scope"]).split()) == {target["enabled_permission"]},
        "disabled target role permission entered JWT scope",
    )
    target_sid = _token_sid(target_login)
    sensitive_values.update((str(target_login["access_token"]), str(target_login["refresh_token"]), target_sid))

    paths = _expect(client.get("/openapi.json"), 200, "OpenAPI document")["paths"]
    for path, methods in {
        "/admin/roles": {"get", "post"},
        "/admin/roles/{role_id}": {"get", "patch"},
        "/admin/roles/{role_id}/disable": {"post"},
        "/admin/roles/{role_id}/enable": {"post"},
        "/admin/roles/{role_id}/users": {"get"},
        "/admin/users/{user_id}/roles": {"get", "put"},
    }.items():
        _assert(path in paths and methods <= set(paths[path]), f"OpenAPI is missing {path} methods")
        for method in methods:
            _assert(bool(paths[path][method].get("security")), f"OpenAPI {method.upper()} {path} has no security")

    _expect(client.get("/admin/roles"), 401, "unauthenticated role list")
    none_token = str(logins["none"]["access_token"])
    _expect(client.get("/admin/roles", headers=_authorization(none_token)), 403, "no-role-permission boundary")

    create_payload = {"name": f"phase5_role_{uuid4().hex[:8]}", "description": "Phase five role"}
    created = _expect(
        client.post(
            "/admin/roles",
            headers=_authorization(str(logins["role:create"]["access_token"]), "role-phase5-create"),
            json=create_payload,
        ),
        201,
        "create role",
    )
    role_id = int(created["id"])
    _assert(created["version"] == 1 and created["is_enabled"] is True, "created role state is incorrect")
    _assert_no_sensitive_fields(created, "create role response")

    permission_actions: dict[str, Callable[[str], httpx.Response]] = {
        "role:read": lambda token: client.get("/admin/roles", headers=_authorization(token)),
        "role:create": lambda token: client.post("/admin/roles", headers=_authorization(token), json=create_payload),
        "role:update": lambda token: client.patch(
            f"/admin/roles/{role_id}",
            headers=_authorization(token),
            json={"description": created["description"], "version": created["version"]},
        ),
        "role:disable": lambda token: client.post(
            f"/admin/roles/{role_id}/disable",
            headers=_authorization(token),
            json={"reason": "boundary"},
        ),
        "role:enable": lambda token: client.post(f"/admin/roles/{role_id}/enable", headers=_authorization(token)),
        "user:assign_roles": lambda token: client.put(
            f"/admin/users/{target['id']}/roles",
            headers=_authorization(token),
            json={
                "role_ids": [target["enabled_role_id"], target["disabled_role_id"]],
                "version": 1,
            },
        ),
    }
    permission_denials = 2
    for required_permission, action in permission_actions.items():
        wrong_permission = next(permission for permission in ROLE_PERMISSIONS if permission != required_permission)
        _expect(
            action(str(logins[wrong_permission]["access_token"])),
            403,
            f"{wrong_permission} cannot use {required_permission} endpoint",
        )
        permission_denials += 1

    read_headers = _authorization(str(logins["role:read"]["access_token"]))
    listed = _expect(
        client.get("/admin/roles", headers=read_headers, params={"keyword": create_payload["name"], "is_enabled": True}),
        200,
        "list roles",
    )
    _assert(listed["total"] == 1 and listed["items"][0]["id"] == role_id, "role list filtering failed")
    detail = _expect(client.get(f"/admin/roles/{role_id}", headers=read_headers), 200, "role details")
    _assert_no_sensitive_fields(detail, "role details")

    unchanged = _expect(
        client.patch(
            f"/admin/roles/{role_id}",
            headers=_authorization(str(logins["role:update"]["access_token"]), "role-phase5-update-no-change"),
            json={"description": created["description"], "version": 1},
        ),
        200,
        "no-change role update",
    )
    _assert(unchanged["changed"] is False and unchanged["version"] == 1, "no-change update changed the role")
    updated = _expect(
        client.patch(
            f"/admin/roles/{role_id}",
            headers=_authorization(str(logins["role:update"]["access_token"]), "role-phase5-update"),
            json={"description": "Updated phase five role", "version": 1},
        ),
        200,
        "update role",
    )
    _assert(updated["changed"] is True and updated["version"] == 2, "role update failed")
    conflict = _expect(
        client.patch(
            f"/admin/roles/{role_id}",
            headers=_authorization(str(logins["role:update"]["access_token"])),
            json={"description": "Stale", "version": 1},
        ),
        409,
        "stale role update",
    )
    _assert(conflict.get("detail") == "ROLE_VERSION_CONFLICT", "stale role update returned the wrong error")

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO user_roles (user_id, role_id) VALUES (:user_id, :role_id)"),
            {"user_id": target["id"], "role_id": role_id},
        )
    associated = _expect(
        client.get(f"/admin/roles/{role_id}/users", headers=read_headers),
        200,
        "list role users",
    )
    _assert(associated["total"] == 1 and associated["items"][0]["id"] == target["id"], "role user list is incorrect")
    _assert_no_sensitive_fields(associated, "role user list")

    disabled = _expect(
        client.post(
            f"/admin/roles/{role_id}/disable",
            headers=_authorization(str(logins["role:disable"]["access_token"]), "role-phase5-disable"),
            json={"reason": "role phase 5 maintenance"},
        ),
        200,
        "disable role",
    )
    _assert(disabled["changed"] is True and disabled["version"] == 3, "role disable failed")
    _assert(disabled["revoked_sessions"] == 1, "role disable did not revoke the target session")
    _assert_session_revoked(engine, redis_client, env["SESSION_PREFIX"], target_sid, "role_disabled", 86_400)
    _assert_access_and_refresh_revoked(
        client,
        str(target_login["access_token"]),
        str(target_login["refresh_token"]),
    )
    with engine.connect() as connection:
        association_counts = connection.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM user_roles WHERE role_id = :role_id) "
                "+ (SELECT count(*) FROM role_permissions WHERE role_id = :role_id)"
            ),
            {"role_id": role_id},
        ).scalar_one()
    _assert(association_counts == 1, "disabling the role removed an association")

    target_without_disabled = _login(client, target["email"], password, env)
    disabled_claims = target_without_disabled["verified_claims"]
    _assert(create_payload["name"] not in disabled_claims["roles"], "disabled role entered new JWT roles")
    _assert_no_sensitive_fields({"roles": disabled_claims["roles"]}, "disabled role claims")
    sensitive_values.update(
        (
            str(target_without_disabled["access_token"]),
            str(target_without_disabled["refresh_token"]),
            _token_sid(target_without_disabled),
        )
    )

    disabled_again = _expect(
        client.post(
            f"/admin/roles/{role_id}/disable",
            headers=_authorization(str(logins["role:disable"]["access_token"])),
            json={"reason": "must not replace"},
        ),
        200,
        "repeat role disable",
    )
    _assert(disabled_again["changed"] is False and disabled_again["version"] == 3, "repeat disable changed the role")
    _assert(disabled_again["disabled_reason"] == "role phase 5 maintenance", "repeat disable replaced the reason")

    enabled = _expect(
        client.post(
            f"/admin/roles/{role_id}/enable",
            headers=_authorization(str(logins["role:enable"]["access_token"]), "role-phase5-enable"),
        ),
        200,
        "enable role",
    )
    _assert(enabled["changed"] is True and enabled["version"] == 4, "role enable failed")
    enabled_again = _expect(
        client.post(
            f"/admin/roles/{role_id}/enable",
            headers=_authorization(str(logins["role:enable"]["access_token"])),
        ),
        200,
        "repeat role enable",
    )
    _assert(enabled_again["changed"] is False and enabled_again["version"] == 4, "repeat enable changed the role")

    target_with_reenabled = _login(client, target["email"], password, env)
    reenabled_claims = target_with_reenabled["verified_claims"]
    _assert(create_payload["name"] in reenabled_claims["roles"], "re-enabled role did not enter new JWT roles")
    sensitive_values.update(
        (
            str(target_with_reenabled["access_token"]),
            str(target_with_reenabled["refresh_token"]),
            _token_sid(target_with_reenabled),
        )
    )

    user_read_token = str(logins["user:read"]["access_token"])
    current_roles = _expect(
        client.get(f"/admin/users/{target['id']}/roles", headers=_authorization(user_read_token)),
        200,
        "get user roles",
    )
    _assert(current_roles["changed"] is False, "user role query reported a change")
    _assert_no_sensitive_fields(current_roles, "user role query")

    assignment_login = target_with_reenabled
    assignment_sid = _token_sid(assignment_login)
    replaced = _expect(
        client.put(
            f"/admin/users/{target['id']}/roles",
            headers=_authorization(
                str(logins["user:assign_roles"]["access_token"]),
                "role-phase5-assignment",
            ),
            json={
                "role_ids": [target["disabled_role_id"], role_id],
                "version": current_roles["version"],
            },
        ),
        200,
        "replace user roles",
    )
    _assert(replaced["changed"] is True and replaced["version"] == current_roles["version"] + 1, "role replacement failed")
    _assert(replaced["revoked_sessions"] >= 1, "role replacement did not revoke active sessions")
    _assert_session_revoked(engine, redis_client, env["SESSION_PREFIX"], assignment_sid, "user_roles_changed", 86_400)
    _assert_access_and_refresh_revoked(
        client,
        str(assignment_login["access_token"]),
        str(assignment_login["refresh_token"]),
    )

    final_target_login = _login(client, target["email"], password, env)
    final_claims = final_target_login["verified_claims"]
    _assert(final_claims["roles"] == [create_payload["name"]], "re-login did not reflect the final enabled role set")
    _assert(final_claims["scope"] == "", "final target token unexpectedly retained permissions")
    sensitive_values.update(
        (
            str(final_target_login["access_token"]),
            str(final_target_login["refresh_token"]),
            _token_sid(final_target_login),
        )
    )

    cannot_add_disabled = _expect(
        client.put(
            f"/admin/users/{target['id']}/roles",
            headers=_authorization(str(logins["user:assign_roles"]["access_token"])),
            json={
                "role_ids": [
                    role_id,
                    target["disabled_role_id"],
                    target["unassigned_disabled_role_id"],
                ],
                "version": replaced["version"],
            },
        ),
        409,
        "add disabled role",
    )
    _assert(cannot_add_disabled.get("detail") == "ROLE_DISABLED", "disabled role assignment returned the wrong error")
    missing_role = _expect(
        client.put(
            f"/admin/users/{target['id']}/roles",
            headers=_authorization(str(logins["user:assign_roles"]["access_token"])),
            json={"role_ids": [999_999_999], "version": replaced["version"]},
        ),
        404,
        "assign missing role",
    )
    _assert(missing_role.get("detail") == "ROLE_NOT_FOUND", "missing role returned the wrong error")
    stale_user = _expect(
        client.put(
            f"/admin/users/{target['id']}/roles",
            headers=_authorization(str(logins["user:assign_roles"]["access_token"])),
            json={"role_ids": [role_id], "version": 1},
        ),
        409,
        "stale user role assignment",
    )
    _assert(stale_user.get("detail") == "USER_VERSION_CONFLICT", "stale assignment returned the wrong error")
    duplicate_ids = client.put(
        f"/admin/users/{target['id']}/roles",
        headers=_authorization(str(logins["user:assign_roles"]["access_token"])),
        json={"role_ids": [role_id, role_id], "version": replaced["version"]},
    )
    _expect(duplicate_ids, 422, "duplicate role IDs")

    admin_protected = _expect(
        client.post(
            "/admin/roles",
            headers=_authorization(str(logins["role:create"]["access_token"])),
            json={"name": f"temporary_admin_check_{uuid4().hex[:8]}"},
        ),
        201,
        "create role for protected check",
    )
    _assert(admin_protected["name"].startswith("temporary_admin_check_"), "protected check setup failed")
    with engine.connect() as connection:
        admin_role_id = connection.execute(text("SELECT id FROM roles WHERE name = 'admin'" )).scalar_one()
    protected = _expect(
        client.post(
            f"/admin/roles/{admin_role_id}/disable",
            headers=_authorization(str(logins["role:disable"]["access_token"])),
            json={"reason": "forbidden"},
        ),
        409,
        "disable admin role",
    )
    _assert(protected.get("detail") == "PROTECTED_ROLE_OPERATION", "admin protection returned the wrong error")

    with engine.connect() as connection:
        audits = connection.execute(
            text(
                "SELECT actor_user_id, action, target_type, target_id, reason, changes_json, request_id "
                "FROM audit_events WHERE "
                "(target_type = 'role' AND target_id = :role_id) "
                "OR (target_type = 'user' AND target_id = :target_id) ORDER BY id"
            ),
            {"role_id": role_id, "target_id": target["id"]},
        ).mappings().all()
        seed_counts = connection.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM permissions WHERE name = ANY(:permissions)) AS permissions, "
                "(SELECT count(*) FROM role_permissions rp "
                "JOIN roles r ON r.id = rp.role_id "
                "JOIN permissions p ON p.id = rp.permission_id "
                "WHERE r.name = 'admin' AND p.name = ANY(:permissions)) AS associations"
            ),
            {"permissions": list(ROLE_PERMISSIONS)},
        ).mappings().one()
    expected_actions = {"role.created", "role.updated", "role.disabled", "role.enabled", "user.roles_assigned"}
    _assert(expected_actions <= {audit["action"] for audit in audits}, "role lifecycle audits are incomplete")
    expected_request_ids = {
        "role-phase5-create",
        "role-phase5-update",
        "role-phase5-disable",
        "role-phase5-enable",
        "role-phase5-assignment",
    }
    _assert(expected_request_ids <= {audit["request_id"] for audit in audits}, "role audit request IDs are incomplete")
    _assert(seed_counts["permissions"] == len(ROLE_PERMISSIONS), "role permission seed is incomplete")
    _assert(seed_counts["associations"] == len(ROLE_PERMISSIONS), "admin role permission seed is incomplete")

    serialized_audits = json.dumps([dict(audit) for audit in audits], default=str)
    for secret in sensitive_values:
        _assert(secret not in serialized_audits, "role audit exposed sensitive authentication data")
    _assert("hashed_password" not in serialized_audits, "role audit exposed a password hash field")
    _assert("access_token" not in serialized_audits, "role audit exposed an access token field")
    _assert("refresh_token" not in serialized_audits, "role audit exposed a refresh token field")

    return (
        {
            "role_id": role_id,
            "permissions_verified": len(ROLE_PERMISSIONS),
            "permission_denials_verified": permission_denials,
            "lifecycle_audits_verified": len(expected_actions),
            "request_ids_verified": len(expected_request_ids),
            "redis_revocations_verified": 2,
            "old_sessions_rejected": True,
            "disabled_role_claim_filter": True,
            "reenabled_role_claim_restore": True,
            "user_role_replacement_verified": True,
            "seed_idempotency_verified": True,
        },
        sensitive_values,
    )


def run_http_validation(config: RolePhase5Config) -> dict[str, Any]:
    with temporary_postgres_database(config, "http") as database:
        suffix = database.name.rsplit("_", 1)[-1]
        redis_prefix = f"auth:role-phase5:{suffix}:"
        redis_client: Redis = Redis.from_url(config.redis_url, decode_responses=True)
        redis_client.ping()
        _clean_redis_namespace(redis_client, redis_prefix)
        env = _runtime_env(database.url, config.redis_url, redis_prefix)
        _alembic(database.url, "upgrade", "head")
        _seed(env)
        _seed(env)
        _sync_permissions(env)
        fixtures = _create_http_fixtures(database.url)
        engine = create_engine(database.url, poolclass=NullPool)
        port = config.api_port or _find_available_port(config.api_host)
        base_url = f"http://{config.api_host}:{port}"
        api_log = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
        process = subprocess.Popen(
            (
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                config.api_host,
                "--port",
                str(port),
                "--log-level",
                "info",
            ),
            cwd=ROOT_DIR,
            env=env,
            text=True,
            stdout=api_log,
            stderr=subprocess.STDOUT,
        )
        result: dict[str, Any] | None = None
        sensitive_values: set[str] = {
            env["JWT_PRIVATE_KEY"],
            env["JWT_PUBLIC_KEY"],
            str(fixtures["password"]),
            "password123",
        }
        flow_error: Exception | None = None
        try:
            _wait_for_api(base_url, process)
            with httpx.Client(base_url=base_url, timeout=20) as client:
                result, flow_secrets = _run_http_role_flow(client, engine, redis_client, env, fixtures)
                sensitive_values.update(flow_secrets)
        except Exception as exc:
            flow_error = exc
        finally:
            engine.dispose()
            _stop_process(process)
            api_log.seek(0)
            log_text = api_log.read()
            api_log.close()
            _clean_redis_namespace(redis_client, redis_prefix)
            remaining_keys = list(redis_client.scan_iter(match=f"{redis_prefix}*"))
            redis_client.close()

        _assert(not remaining_keys, "temporary Redis prefix was not cleaned")
        if flow_error is None:
            for secret in sensitive_values:
                _assert(secret not in log_text, "API logs exposed sensitive role validation data")
        else:
            safe_error = _redact(str(flow_error), [*sensitive_values, *_database_secrets(database.url)])
            safe_log_tail = _redact("\n".join(log_text.splitlines()[-30:]), list(sensitive_values))
            raise RolePhase5ValidationError(
                f"role management HTTP validation failed: {safe_error}\nAPI log tail:\n{safe_log_tail}"
            ) from flow_error

        _assert(result is not None, "role HTTP validation produced no result")
        return {
            "database": database.name,
            **result,
            "real_jwt_permissions": True,
            "sensitive_log_scan": "clean",
            "temporary_resources_cleaned": True,
        }


def run_all_validations(config: RolePhase5Config) -> dict[str, dict[str, Any]]:
    return {
        "migration": run_migration_validation(config),
        "concurrency": run_concurrency_validation(config),
        "http": run_http_validation(config),
    }


def _print_report(name: str, report: dict[str, Any]) -> None:
    print(f"[PASS] {name}: {json.dumps(report, sort_keys=True)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate role management phase 5 with isolated PostgreSQL and Redis")
    parser.add_argument(
        "--only",
        choices=("all", "migration", "concurrency", "http"),
        default="all",
        help="run all validations or one validation group",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = RolePhase5Config.from_env()
        if args.only in {"all", "migration"}:
            _print_report("Role Alembic migration roundtrip", run_migration_validation(config))
        if args.only in {"all", "concurrency"}:
            _print_report("Role PostgreSQL concurrency", run_concurrency_validation(config))
        if args.only in {"all", "http"}:
            _print_report("Role JWT, Redis, and HTTP lifecycle", run_http_validation(config))
    except (OSError, ValueError, RolePhase5ValidationError) as exc:
        database_url = os.getenv("ROLE_PHASE5_ADMIN_DATABASE_URL", "")
        safe_error = _redact(str(exc), _database_secrets(database_url) if database_url else ())
        print(f"[FAIL] role phase 5 validation: {safe_error}", file=sys.stderr)
        return 1
    print("Role phase 5 validation completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
