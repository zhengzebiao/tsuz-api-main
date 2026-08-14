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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession, sessionmaker
from sqlalchemy.pool import NullPool

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

DEFAULT_ADMIN_DATABASE_URL = (
    "postgresql+psycopg://test_user:test_password@127.0.0.1:55432/postgres"
)
DEFAULT_REDIS_URL = "redis://127.0.0.1:56379/15"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
HEAD_REVISION = "0005_permission_management"
PERMISSION_MANAGEMENT_PERMISSIONS = (
    "permission:read",
    "permission:update",
    "permission:disable",
    "permission:enable",
)
TARGET_PERMISSION = "app:read"
SENSITIVE_RESPONSE_FIELDS = {
    "access_token",
    "refresh_token",
    "password",
    "new_password",
    "hashed_password",
    "sid",
}


class PermissionPhase5ValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PermissionPhase5Config:
    admin_database_url: str
    redis_url: str
    api_host: str
    api_port: int
    allow_remote: bool
    allow_default_ports: bool

    @classmethod
    def from_env(cls) -> PermissionPhase5Config:
        config = cls(
            admin_database_url=os.getenv(
                "PERMISSION_PHASE5_ADMIN_DATABASE_URL",
                DEFAULT_ADMIN_DATABASE_URL,
            ),
            redis_url=os.getenv("PERMISSION_PHASE5_REDIS_URL", DEFAULT_REDIS_URL),
            api_host=os.getenv("PERMISSION_PHASE5_API_HOST", "127.0.0.1"),
            api_port=int(os.getenv("PERMISSION_PHASE5_API_PORT", "0")),
            allow_remote=os.getenv("PERMISSION_PHASE5_ALLOW_REMOTE", "0") == "1",
            allow_default_ports=(
                os.getenv("PERMISSION_PHASE5_ALLOW_DEFAULT_PORTS", "0") == "1"
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        database_url = make_url(self.admin_database_url)
        redis_url = urlparse(self.redis_url)
        if not database_url.drivername.startswith("postgresql"):
            raise PermissionPhase5ValidationError(
                "PERMISSION_PHASE5_ADMIN_DATABASE_URL must use PostgreSQL"
            )
        if redis_url.scheme not in {"redis", "rediss"}:
            raise PermissionPhase5ValidationError(
                "PERMISSION_PHASE5_REDIS_URL must use redis:// or rediss://"
            )
        if not 0 <= self.api_port <= 65535:
            raise PermissionPhase5ValidationError(
                "PERMISSION_PHASE5_API_PORT must be between 0 and 65535"
            )
        if not self.allow_remote:
            hosts = {database_url.host, redis_url.hostname, self.api_host}
            remote_hosts = sorted(
                host for host in hosts if host and host not in LOCAL_HOSTS
            )
            if remote_hosts:
                raise PermissionPhase5ValidationError(
                    "permission phase 5 validation only allows local services by default; "
                    "set PERMISSION_PHASE5_ALLOW_REMOTE=1 for an explicitly approved "
                    "isolated environment"
                )
        if not self.allow_default_ports:
            database_port = database_url.port or 5432
            redis_port = redis_url.port or 6379
            if database_port == 5432 or redis_port == 6379:
                raise PermissionPhase5ValidationError(
                    "permission phase 5 validation refuses PostgreSQL 5432 or Redis 6379 "
                    "by default; use dedicated temporary ports or explicitly set "
                    "PERMISSION_PHASE5_ALLOW_DEFAULT_PORTS=1"
                )


@dataclass(frozen=True)
class TemporaryDatabase:
    name: str
    url: str


T = TypeVar("T")


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise PermissionPhase5ValidationError(message)


def _redact(value: str, secrets: Sequence[str] = ()) -> str:
    redacted = value
    for secret in sorted((secret for secret in secrets if secret), key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _database_secrets(database_url: str) -> list[str]:
    url = make_url(database_url)
    return [url.password or "", database_url]


def _redis_secrets(redis_url: str) -> list[str]:
    parsed = urlparse(redis_url)
    return [parsed.password or "", redis_url]


def _run_command(
    command: Sequence[str],
    *,
    env: dict[str, str],
    secrets: Sequence[str] = (),
    expected_exit_codes: frozenset[int] = frozenset({0}),
) -> tuple[int, str]:
    completed = subprocess.run(
        list(command),
        cwd=ROOT_DIR,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    output = "\n".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    if completed.returncode not in expected_exit_codes:
        safe_output = _redact(output, secrets)
        raise PermissionPhase5ValidationError(
            f"command failed ({' '.join(command)}):\n{safe_output}"
        )
    return completed.returncode, output


def _alembic(database_url: str, *arguments: str) -> str:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    return _run_command(
        (sys.executable, "-m", "alembic", *arguments),
        env=env,
        secrets=_database_secrets(database_url),
    )[1]


def _database_url_for(admin_database_url: str, database_name: str) -> str:
    return (
        make_url(admin_database_url)
        .set(database=database_name)
        .render_as_string(hide_password=False)
    )


def _quoted_identifier(identifier: str) -> str:
    if not identifier.replace("_", "").isalnum():
        raise PermissionPhase5ValidationError(
            "generated database name contains invalid characters"
        )
    return f'"{identifier}"'


@contextmanager
def temporary_postgres_database(
    config: PermissionPhase5Config,
    purpose: str,
) -> Iterator[TemporaryDatabase]:
    suffix = uuid4().hex[:12]
    database_name = f"tsuz_permission_phase5_{purpose}_{suffix}"
    admin_engine = create_engine(
        config.admin_database_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    created = False
    try:
        with admin_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            connection.exec_driver_sql(
                f"CREATE DATABASE {_quoted_identifier(database_name)}"
            )
            created = True
        yield TemporaryDatabase(
            database_name,
            _database_url_for(config.admin_database_url, database_name),
        )
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
                    connection.exec_driver_sql(
                        f"DROP DATABASE IF EXISTS {_quoted_identifier(database_name)}"
                    )
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
            "APP_ENV": "permission-phase5",
            "DEBUG": "false",
            "LOG_LEVEL": "info",
            "LOG_FORMAT": "json",
            "DATABASE_URL": database_url,
            "REDIS_URL": redis_url,
            "REDIS_KEY_PREFIX": redis_prefix,
            "JWT_ALGORITHM": "RS256",
            "JWT_ISSUER": "auth-service-permission-phase5",
            "JWT_AUDIENCE": "backend-api-permission-phase5",
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
        secrets=[
            *_database_secrets(env["DATABASE_URL"]),
            *_redis_secrets(env["REDIS_URL"]),
        ],
    )


def _permission_sync(
    env: dict[str, str],
    *arguments: str,
    expected_exit_codes: frozenset[int] = frozenset({0}),
) -> dict[str, Any]:
    exit_code, output = _run_command(
        (sys.executable, "-m", "app.commands.sync_permissions", *arguments),
        env=env,
        secrets=[
            *_database_secrets(env["DATABASE_URL"]),
            *_redis_secrets(env["REDIS_URL"]),
            env.get("JWT_PRIVATE_KEY", ""),
        ],
        expected_exit_codes=expected_exit_codes,
    )
    json_reports: list[dict[str, Any]] = []
    for line in output.splitlines():
        if not line.startswith("{"):
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and not {"level", "logger", "message"} <= set(candidate):
            json_reports.append(candidate)
    _assert(bool(json_reports), "permission sync did not emit a JSON report")
    report = json_reports[-1]
    report["exit_code"] = exit_code
    return report


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


def run_migration_validation(config: PermissionPhase5Config) -> dict[str, Any]:
    with temporary_postgres_database(config, "migration") as database:
        _alembic(database.url, "upgrade", "0004_role_management")
        engine = create_engine(database.url, poolclass=NullPool)
        legacy_permission_name = f"legacy:permission_{uuid4().hex[:8]}"
        legacy_role_name = f"legacy_permission_role_{uuid4().hex[:8]}"
        try:
            with engine.begin() as connection:
                role_id = int(
                    connection.execute(
                        text("INSERT INTO roles (name) VALUES (:name) RETURNING id"),
                        {"name": legacy_role_name},
                    ).scalar_one()
                )
                permission_id = int(
                    connection.execute(
                        text(
                            "INSERT INTO permissions (name, description) "
                            "VALUES (:name, 'Legacy permission description') RETURNING id"
                        ),
                        {"name": legacy_permission_name},
                    ).scalar_one()
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

        _alembic(database.url, "upgrade", HEAD_REVISION)
        engine = create_engine(database.url, poolclass=NullPool)
        permission_columns = {
            "id",
            "name",
            "display_name",
            "description",
            "is_declared",
            "is_enabled",
            "disabled_at",
            "disabled_reason",
            "missing_at",
            "created_at",
            "updated_at",
            "version",
        }
        permission_indexes = {
            "ix_permissions_id",
            "ix_permissions_name",
            "ix_permissions_is_declared",
            "ix_permissions_is_enabled",
        }
        try:
            inspector = inspect(engine)
            columns = {
                column["name"]: column
                for column in inspector.get_columns("permissions")
            }
            indexes = {
                index["name"]: index
                for index in inspector.get_indexes("permissions")
            }
            endpoint_columns = {
                column["name"]: column
                for column in inspector.get_columns("permission_endpoints")
            }
            endpoint_indexes = {
                index["name"]: index
                for index in inspector.get_indexes("permission_endpoints")
            }
            endpoint_primary_key = inspector.get_pk_constraint(
                "permission_endpoints"
            )
            endpoint_foreign_keys = inspector.get_foreign_keys(
                "permission_endpoints"
            )
            _assert(
                set(columns) == permission_columns,
                "permissions table columns do not match the model",
            )
            for column_name in {
                "id",
                "name",
                "display_name",
                "description",
                "is_declared",
                "is_enabled",
                "created_at",
                "updated_at",
                "version",
            }:
                _assert(
                    columns[column_name]["nullable"] is False,
                    f"permissions.{column_name} must be NOT NULL",
                )
            for column_name in {"disabled_at", "disabled_reason", "missing_at"}:
                _assert(
                    columns[column_name]["nullable"] is True,
                    f"permissions.{column_name} must be nullable",
                )
            _assert(
                permission_indexes <= set(indexes),
                "permissions indexes are incomplete",
            )
            _assert(
                indexes["ix_permissions_name"]["unique"] is True,
                "permission name index is not unique",
            )
            _assert(
                indexes["ix_permissions_is_declared"]["unique"] is False,
                "permission declared index must not be unique",
            )
            _assert(
                indexes["ix_permissions_is_enabled"]["unique"] is False,
                "permission enabled index must not be unique",
            )
            _assert(
                set(endpoint_columns)
                == {"permission_id", "http_method", "path", "route_name"},
                "permission endpoint columns are incorrect",
            )
            _assert(
                all(column["nullable"] is False for column in endpoint_columns.values()),
                "permission endpoint columns must be NOT NULL",
            )
            _assert(
                endpoint_primary_key["constrained_columns"]
                == ["permission_id", "http_method", "path"],
                "permission endpoint primary key is incorrect",
            )
            _assert(
                len(endpoint_foreign_keys) == 1
                and endpoint_foreign_keys[0]["referred_table"] == "permissions"
                and endpoint_foreign_keys[0]["referred_columns"] == ["id"]
                and endpoint_foreign_keys[0]["options"].get("ondelete") == "CASCADE",
                "permission endpoint foreign key is incorrect",
            )
            _assert(
                endpoint_indexes["ix_permission_endpoints_http_method_path"][
                    "column_names"
                ]
                == ["http_method", "path"],
                "permission endpoint index is incorrect",
            )

            with engine.begin() as connection:
                legacy = connection.execute(
                    text(
                        "SELECT id, name, display_name, description, is_declared, "
                        "is_enabled, disabled_at, disabled_reason, missing_at, "
                        "created_at, updated_at, version FROM permissions "
                        "WHERE id = :permission_id"
                    ),
                    {"permission_id": permission_id},
                ).mappings().one()
                association_count = int(
                    connection.execute(
                        text(
                            "SELECT count(*) FROM role_permissions "
                            "WHERE role_id = :role_id AND permission_id = :permission_id"
                        ),
                        {"role_id": role_id, "permission_id": permission_id},
                    ).scalar_one()
                )
                inserted = connection.execute(
                    text(
                        "INSERT INTO permissions (name, description) VALUES (:name, '') "
                        "RETURNING id, display_name, is_declared, is_enabled, "
                        "created_at, updated_at, version"
                    ),
                    {"name": f"new:permission_{uuid4().hex[:8]}"},
                ).mappings().one()
                connection.execute(
                    text(
                        "INSERT INTO permission_endpoints "
                        "(permission_id, http_method, path, route_name) VALUES "
                        "(:permission_id, 'GET', '/admin/permissions', 'list_permissions')"
                    ),
                    {"permission_id": permission_id},
                )
            _assert(legacy["id"] == permission_id, "legacy permission ID changed")
            _assert(
                legacy["name"] == legacy_permission_name
                and legacy["display_name"] == legacy_permission_name,
                "legacy permission name was not preserved and backfilled",
            )
            _assert(
                legacy["description"] == "Legacy permission description",
                "legacy permission description changed",
            )
            _assert(
                legacy["is_declared"] is True
                and legacy["is_enabled"] is True
                and legacy["version"] == 1,
                "legacy permission state defaults are incorrect",
            )
            _assert(
                legacy["disabled_at"] is None
                and legacy["disabled_reason"] is None
                and legacy["missing_at"] is None,
                "legacy permission nullable state is incorrect",
            )
            _assert(
                legacy["created_at"] is not None and legacy["updated_at"] is not None,
                "legacy permission timestamps are missing",
            )
            _assert(association_count == 1, "legacy role permission link was lost")
            _assert(
                inserted["id"] == permission_id + 1,
                "permission sequence did not continue from the legacy ID",
            )
            _assert(
                inserted["display_name"] == ""
                and inserted["is_declared"] is True
                and inserted["is_enabled"] is True
                and inserted["version"] == 1,
                "new permission database defaults are incorrect",
            )
            duplicate_rejected = False
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO permission_endpoints "
                            "(permission_id, http_method, path, route_name) VALUES "
                            "(:permission_id, 'GET', '/admin/permissions', 'duplicate')"
                        ),
                        {"permission_id": permission_id},
                    )
            except IntegrityError:
                duplicate_rejected = True
            _assert(duplicate_rejected, "duplicate permission endpoint was accepted")
        finally:
            engine.dispose()

        check_output = _alembic(database.url, "check")
        _assert(
            "No new upgrade operations detected" in check_output,
            "Alembic metadata check is not clean",
        )
        _alembic(database.url, "downgrade", "0004_role_management")
        engine = create_engine(database.url, poolclass=NullPool)
        try:
            inspector = inspect(engine)
            downgraded_columns = {
                column["name"] for column in inspector.get_columns("permissions")
            }
            _assert(
                downgraded_columns == {"id", "name", "description"},
                "permission management columns survived downgrade",
            )
            _assert(
                not inspector.has_table("permission_endpoints"),
                "permission_endpoints survived downgrade",
            )
            with engine.connect() as connection:
                preserved = connection.execute(
                    text(
                        "SELECT p.id, p.name, p.description, count(rp.role_id) AS links "
                        "FROM permissions p LEFT JOIN role_permissions rp "
                        "ON rp.permission_id = p.id WHERE p.id = :permission_id "
                        "GROUP BY p.id, p.name, p.description"
                    ),
                    {"permission_id": permission_id},
                ).mappings().one()
            _assert(
                preserved["id"] == permission_id
                and preserved["name"] == legacy_permission_name
                and preserved["description"] == "Legacy permission description"
                and preserved["links"] == 1,
                "legacy permission data was lost during downgrade",
            )
        finally:
            engine.dispose()

        _alembic(database.url, "upgrade", "head")
        final_check = _alembic(database.url, "check")
        current = _alembic(database.url, "current")
        _assert(HEAD_REVISION in current, "database did not return to permission head")
        _assert(
            "No new upgrade operations detected" in final_check,
            "Alembic metadata check is not clean after re-upgrade",
        )
        engine = create_engine(database.url, poolclass=NullPool)
        try:
            with engine.connect() as connection:
                restored = connection.execute(
                    text(
                        "SELECT id, display_name, is_declared, is_enabled, version "
                        "FROM permissions WHERE id = :permission_id"
                    ),
                    {"permission_id": permission_id},
                ).mappings().one()
                association_count = int(
                    connection.execute(
                        text(
                            "SELECT count(*) FROM role_permissions "
                            "WHERE role_id = :role_id AND permission_id = :permission_id"
                        ),
                        {"role_id": role_id, "permission_id": permission_id},
                    ).scalar_one()
                )
            _assert(
                restored["id"] == permission_id
                and restored["display_name"] == legacy_permission_name
                and restored["is_declared"] is True
                and restored["is_enabled"] is True
                and restored["version"] == 1
                and association_count == 1,
                "re-upgrade did not preserve legacy permission state",
            )
        finally:
            engine.dispose()

        return {
            "database": database.name,
            "current_revision": HEAD_REVISION,
            "alembic_check": "clean",
            "permission_columns_verified": len(permission_columns),
            "permission_indexes_verified": len(permission_indexes),
            "endpoint_columns_verified": 4,
            "legacy_permission_data_preserved": True,
            "role_associations_preserved": True,
            "sequence_preserved": True,
            "duplicate_endpoint_rejected": True,
            "temporary_resources_cleaned": True,
        }


def _wait_until_lock_wait(
    engine: Engine,
    backend_pid: int,
    timeout_seconds: float = 5,
) -> bool:
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
            _assert(
                release_first.wait(timeout=10),
                "timed out waiting to release the first transaction",
            )
            db.commit()
            return result

    def second_task() -> T:
        _assert(
            first_locked.wait(timeout=10),
            "first transaction did not acquire its lock",
        )
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
        waited = _wait_until_lock_wait(engine, second_pid[0])
        release_first.set()
        return (
            first_future.result(timeout=15),
            second_future.result(timeout=15),
            waited,
        )


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
            text(
                "SELECT status, revoked_at, revoked_reason FROM sessions WHERE sid = :sid"
            ),
            {"sid": sid},
        ).mappings().one()
    _assert(session["status"] == "revoked", "session was not revoked in PostgreSQL")
    _assert(session["revoked_at"] is not None, "session revoked_at is missing")
    _assert(
        session["revoked_reason"] == reason,
        "session has the wrong PostgreSQL revocation reason",
    )
    key = f"{session_prefix}{sid}"
    redis_state = redis_client.get(key)
    _assert(
        redis_state == "revoked",
        f"session was not revoked in Redis state={redis_state!r}",
    )
    ttl = int(redis_client.ttl(key))
    _assert(0 < ttl <= expected_ttl, "session Redis TTL is invalid")


def run_concurrency_validation(config: PermissionPhase5Config) -> dict[str, Any]:
    with temporary_postgres_database(config, "concurrency") as database:
        _alembic(database.url, "upgrade", "head")
        engine = create_engine(database.url, poolclass=NullPool)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        redis_prefix = f"auth:permission-phase5:concurrency:{uuid4().hex[:12]}:"
        redis_client: Redis = Redis.from_url(config.redis_url, decode_responses=True)
        redis_client.ping()
        _clean_redis_namespace(redis_client, redis_prefix)

        from app.core.config import settings
        from app.core.redis import get_redis
        from app.core.security import hash_password
        from app.main import create_app
        from app.models.audit_event import AuditEvent
        from app.models.permission import Permission
        from app.models.permission_endpoint import PermissionEndpoint
        from app.models.role import Role, role_permissions, user_roles
        from app.models.session import Session as AuthSession
        from app.models.user import User
        from app.schemas.admin_permission import AdminPermissionUpdate
        from app.seed.__main__ import seed
        from app.services.admin_permission_service import (
            AdminPermissionService,
            PermissionVersionConflictError,
        )
        from app.services.permission_scanner import scan_permission_routes
        from app.services.permission_sync_service import PermissionSyncService

        original_session_prefix = settings.session_prefix
        original_redis_url = settings.redis_url
        try:
            settings.redis_url = config.redis_url
            settings.session_prefix = f"{redis_prefix}session:"
            get_redis.cache_clear()
            catalog = scan_permission_routes(create_app())
            with SessionLocal() as db:
                seed(db)
                db.commit()

            first_locked = threading.Event()
            release_first = threading.Event()
            second_pid_ready = threading.Event()
            release_second = threading.Event()
            second_pid: list[int] = []

            def synchronize(index: int) -> dict[str, int]:
                with SessionLocal() as db:
                    service = PermissionSyncService(db)
                    plan = service.build_plan(catalog)
                    original_lock = service._acquire_advisory_lock

                    def observed_lock() -> None:
                        if index == 1:
                            original_lock()
                            first_locked.set()
                            _assert(
                                release_first.wait(timeout=10),
                                "timed out holding the first advisory lock",
                            )
                            return
                        second_pid.append(
                            int(db.scalar(text("SELECT pg_backend_pid()")))
                        )
                        second_pid_ready.set()
                        _assert(
                            release_second.wait(timeout=10),
                            "timed out releasing the second synchronization",
                        )
                        original_lock()

                    service._acquire_advisory_lock = observed_lock  # type: ignore[method-assign]
                    summary = service.apply_plan(plan)
                    db.commit()
                    return summary.to_dict()

            with ThreadPoolExecutor(max_workers=2) as executor:
                first_future = executor.submit(synchronize, 1)
                _assert(first_locked.wait(timeout=10), "first sync did not lock")
                second_future = executor.submit(synchronize, 2)
                _assert(second_pid_ready.wait(timeout=10), "second sync did not start")
                release_second.set()
                sync_waited = _wait_until_lock_wait(engine, second_pid[0])
                release_first.set()
                first_sync = first_future.result(timeout=15)
                second_sync = second_future.result(timeout=15)
            _assert(sync_waited, "concurrent sync did not wait on advisory lock")
            _assert(
                first_sync["created"] == 25
                and first_sync["endpoint_bindings_added"] == 31
                and first_sync["admin_grants_added"] == 25,
                "first synchronization summary is incorrect",
            )
            _assert(
                sum(
                    second_sync[key]
                    for key in (
                        "created",
                        "restored",
                        "marked_missing",
                        "endpoint_bindings_added",
                        "endpoint_bindings_removed",
                        "admin_grants_added",
                        "sessions_revoked",
                    )
                )
                == 0,
                "second synchronization was not idempotent",
            )

            with SessionLocal() as db:
                admin = db.scalar(select(Role).where(Role.name == "admin"))
                target = db.scalar(
                    select(Permission).where(Permission.name == TARGET_PERMISSION)
                )
                actor = User(
                    email=f"permission-concurrency-actor-{uuid4().hex[:8]}@example.com",
                    hashed_password=hash_password("permission-concurrency-password"),
                    is_active=True,
                    is_blacklisted=False,
                )
                affected = User(
                    email=f"permission-concurrency-target-{uuid4().hex[:8]}@example.com",
                    hashed_password=hash_password("permission-concurrency-password"),
                    is_active=True,
                    is_blacklisted=False,
                )
                first_role = Role(name=f"permission_concurrency_first_{uuid4().hex[:8]}")
                second_role = Role(name=f"permission_concurrency_second_{uuid4().hex[:8]}")
                _assert(admin is not None and target is not None, "sync fixtures are missing")
                db.add_all((actor, affected, first_role, second_role))
                db.flush()
                actor_id = actor.id
                affected_id = affected.id
                target_id = target.id
                target_endpoint_count = int(
                    db.scalar(
                        select(func.count())
                        .select_from(PermissionEndpoint)
                        .where(PermissionEndpoint.permission_id == target_id)
                    )
                    or 0
                )
                for role in (first_role, second_role):
                    db.execute(
                        user_roles.insert().values(
                            user_id=affected_id,
                            role_id=role.id,
                        )
                    )
                    db.execute(
                        role_permissions.insert().values(
                            role_id=role.id,
                            permission_id=target_id,
                        )
                    )
                db.add(
                    AuthSession(
                        sid="permission-concurrency-session",
                        user_id=affected_id,
                        status="active",
                    )
                )
                db.commit()

            disable_first, disable_second, disable_waited = _run_locked_pair(
                engine,
                lambda db: AdminPermissionService(db).disable_permission(
                    target_id,
                    actor_user_id=actor_id,
                    reason="first concurrency reason",
                    request_id="permission-phase5-disable-first",
                ),
                lambda db: AdminPermissionService(db).disable_permission(
                    target_id,
                    actor_user_id=actor_id,
                    reason="second concurrency reason",
                    request_id="permission-phase5-disable-second",
                ),
            )
            _assert(
                disable_waited,
                "concurrent permission disable did not wait on a PostgreSQL row lock",
            )
            _assert(
                disable_first[1] is True
                and disable_first[2] == 1
                and disable_second[1] is False
                and disable_second[2] == 0,
                "permission disable idempotency or DISTINCT revocation failed",
            )
            _assert_session_revoked(
                engine,
                redis_client,
                settings.session_prefix,
                "permission-concurrency-session",
                "permission_disabled",
                settings.refresh_token_expire_days * 86_400,
            )
            with SessionLocal() as db:
                disabled = db.get(Permission, target_id)
                _assert(
                    disabled is not None
                    and disabled.version == 2
                    and disabled.disabled_reason == "first concurrency reason",
                    "concurrent disable produced the wrong permission state",
                )
                disabled_audits = db.scalars(
                    select(AuditEvent).where(
                        AuditEvent.target_type == "permission",
                        AuditEvent.target_id == target_id,
                        AuditEvent.action == "permission.disabled",
                    )
                ).all()
                _assert(
                    len(disabled_audits) == 1,
                    "concurrent disable created duplicate audits",
                )
                _assert(
                    db.scalar(
                        select(func.count())
                        .select_from(PermissionEndpoint)
                        .where(PermissionEndpoint.permission_id == target_id)
                    )
                    == target_endpoint_count,
                    "permission disable removed endpoints",
                )
                _assert(
                    db.scalar(
                        select(func.count())
                        .select_from(role_permissions)
                        .where(role_permissions.c.permission_id == target_id)
                    )
                    == 3,
                    "permission disable removed role associations",
                )

            enable_first, enable_second, enable_waited = _run_locked_pair(
                engine,
                lambda db: AdminPermissionService(db).enable_permission(
                    target_id,
                    actor_user_id=actor_id,
                    request_id="permission-phase5-enable-first",
                ),
                lambda db: AdminPermissionService(db).enable_permission(
                    target_id,
                    actor_user_id=actor_id,
                    request_id="permission-phase5-enable-second",
                ),
            )
            _assert(
                enable_waited,
                "concurrent permission enable did not wait on a PostgreSQL row lock",
            )
            _assert(
                enable_first[1] is True
                and enable_second[1] is False
                and enable_first[2] == enable_second[2] == 0,
                "permission enable idempotency failed",
            )
            with SessionLocal() as db:
                enabled = db.get(Permission, target_id)
                _assert(
                    enabled is not None
                    and enabled.version == 3
                    and enabled.is_enabled is True
                    and enabled.disabled_at is None
                    and enabled.disabled_reason is None,
                    "concurrent enable produced the wrong permission state",
                )
                enabled_audits = db.scalars(
                    select(AuditEvent).where(
                        AuditEvent.target_type == "permission",
                        AuditEvent.target_id == target_id,
                        AuditEvent.action == "permission.enabled",
                    )
                ).all()
                _assert(
                    len(enabled_audits) == 1,
                    "concurrent enable created duplicate audits",
                )

            with SessionLocal() as setup_db:
                editable = setup_db.scalar(
                    select(Permission).where(Permission.name == "app:update")
                )
                _assert(editable is not None, "editable permission is missing")
                editable_id = editable.id
                editable_version = editable.version

            with SessionLocal() as winning_db, SessionLocal() as stale_db:
                stale_db.get(Permission, editable_id)
                updated, changed, _revoked = AdminPermissionService(
                    winning_db
                ).update_permission(
                    editable_id,
                    AdminPermissionUpdate(
                        display_name="Optimistic permission winner",
                        version=editable_version,
                    ),
                    actor_user_id=actor_id,
                    request_id="permission-phase5-update-winner",
                )
                _assert(
                    changed and updated.permission.version == editable_version + 1,
                    "winning permission update failed",
                )
                winning_db.commit()
                conflict = False
                try:
                    AdminPermissionService(stale_db).update_permission(
                        editable_id,
                        AdminPermissionUpdate(
                            display_name="Stale permission loser",
                            version=editable_version,
                        ),
                        actor_user_id=actor_id,
                        request_id="permission-phase5-update-loser",
                    )
                except PermissionVersionConflictError:
                    stale_db.rollback()
                    conflict = True
                _assert(conflict, "stale permission update did not conflict")
            with SessionLocal() as db:
                editable = db.get(Permission, editable_id)
                _assert(
                    editable is not None
                    and editable.display_name == "Optimistic permission winner",
                    "stale permission update overwrote the winner",
                )

            failure_permission_name = "app:create"
            failure_sid = "permission-sync-failure-session"
            with SessionLocal() as db:
                permission = db.scalar(
                    select(Permission).where(Permission.name == failure_permission_name)
                )
                admin = db.scalar(select(Role).where(Role.name == "admin"))
                _assert(
                    permission is not None and admin is not None,
                    "sync rollback fixtures are missing",
                )
                failure_user = User(
                    email=f"permission-sync-failure-{uuid4().hex[:8]}@example.com",
                    hashed_password=hash_password("permission-sync-failure-password"),
                    is_active=True,
                    is_blacklisted=False,
                )
                db.add(failure_user)
                db.flush()
                db.execute(
                    user_roles.insert().values(
                        user_id=failure_user.id,
                        role_id=admin.id,
                    )
                )
                db.add(
                    AuthSession(
                        sid=failure_sid,
                        user_id=failure_user.id,
                        status="active",
                    )
                )
                db.commit()
                failure_permission_id = permission.id
                failure_version = permission.version
                failure_binding_count = int(
                    db.scalar(
                        select(func.count())
                        .select_from(PermissionEndpoint)
                        .where(
                            PermissionEndpoint.permission_id
                            == failure_permission_id
                        )
                    )
                    or 0
                )

            missing_catalog = _without_permission(catalog, failure_permission_name)
            with SessionLocal() as db:
                service = PermissionSyncService(db)
                plan = service.build_plan(missing_catalog)
                _assert(
                    failure_permission_name in plan.marked_missing,
                    "rollback plan did not mark the target permission missing",
                )
                original_write = service.sessions._write_redis_revocation

                def fail_redis(_sid: str) -> None:
                    raise RuntimeError("simulated isolated Redis failure")

                service.sessions._write_redis_revocation = fail_redis  # type: ignore[method-assign]
                redis_failed = False
                try:
                    service.apply_plan(plan)
                except RuntimeError:
                    db.rollback()
                    redis_failed = True
                finally:
                    service.sessions._write_redis_revocation = original_write  # type: ignore[method-assign]
                _assert(redis_failed, "simulated Redis failure did not abort sync")
            with SessionLocal() as db:
                permission = db.get(Permission, failure_permission_id)
                session = db.scalar(
                    select(AuthSession).where(AuthSession.sid == failure_sid)
                )
                _assert(
                    permission is not None
                    and permission.is_declared is True
                    and permission.version == failure_version,
                    "failed sync committed permission state",
                )
                _assert(
                    db.scalar(
                        select(func.count())
                        .select_from(PermissionEndpoint)
                        .where(
                            PermissionEndpoint.permission_id
                            == failure_permission_id
                        )
                    )
                    == failure_binding_count,
                    "failed sync committed endpoint changes",
                )
                _assert(
                    session is not None and session.status == "active",
                    "failed sync committed database session revocation",
                )
            with SessionLocal() as db:
                service = PermissionSyncService(db)
                retry_summary = service.apply_plan(
                    service.build_plan(missing_catalog)
                )
                db.commit()
            _assert(
                retry_summary.marked_missing == 1
                and retry_summary.sessions_revoked == 1,
                "sync retry did not apply exactly once",
            )
            _assert_session_revoked(
                engine,
                redis_client,
                settings.session_prefix,
                failure_sid,
                "permission_sync",
                settings.refresh_token_expire_days * 86_400,
            )
            with SessionLocal() as db:
                no_change = PermissionSyncService(db).build_plan(missing_catalog)
                _assert(
                    no_change.has_changes is False,
                    "sync retry did not converge to zero differences",
                )

            with SessionLocal() as db:
                final_plan = PermissionSyncService(db).build_plan(catalog)
                _assert(
                    failure_permission_name in final_plan.restored,
                    "full catalog did not detect the intentionally missing permission",
                )

            return {
                "database": database.name,
                "advisory_lock_waits_verified": 1,
                "row_lock_waits_verified": 2,
                "sync_counts_verified": [25, 31, 25],
                "disable_changes": [disable_first[1], disable_second[1]],
                "enable_changes": [enable_first[1], enable_second[1]],
                "distinct_session_revocations": disable_first[2],
                "permission_update_conflict": "PERMISSION_VERSION_CONFLICT",
                "redis_failure_rollback_verified": True,
                "sync_retry_idempotency_verified": True,
                "associations_consistent": True,
                "temporary_resources_cleaned": True,
            }
        finally:
            settings.session_prefix = original_session_prefix
            settings.redis_url = original_redis_url
            get_redis.cache_clear()
            _clean_redis_namespace(redis_client, redis_prefix)
            redis_client.close()
            engine.dispose()


def _without_permission(scan_result: Any, permission_name: str) -> Any:
    from app.services.permission_scanner import PermissionScanResult, ScannedRoute

    bindings = tuple(
        binding
        for binding in scan_result.bindings
        if binding.permission_name != permission_name
    )
    routes = tuple(
        ScannedRoute(
            http_method=route.http_method,
            path=route.path,
            route_name=route.route_name,
            required_permissions=tuple(
                name
                for name in route.required_permissions
                if name != permission_name
            ),
        )
        for route in scan_result.routes
    )
    return PermissionScanResult(
        permission_names=tuple(
            name for name in scan_result.permission_names if name != permission_name
        ),
        bindings=bindings,
        routes=routes,
    )


def _find_available_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _wait_for_api(
    base_url: str,
    process: subprocess.Popen[str],
    timeout_seconds: float = 30,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise PermissionPhase5ValidationError(
                "permission phase 5 API process exited before becoming healthy"
            )
        try:
            if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise PermissionPhase5ValidationError(
        "timed out waiting for the permission phase 5 API process"
    )


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
        return {
            key: "[REDACTED]" if key in SENSITIVE_RESPONSE_FIELDS else value
            for key, value in body.items()
        }
    return body


def _expect(
    response: httpx.Response,
    expected_status: int,
    context: str,
) -> dict[str, Any]:
    if response.status_code != expected_status:
        raise PermissionPhase5ValidationError(
            f"{context} returned {response.status_code}, expected {expected_status}: "
            f"{_safe_response_body(response)}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise PermissionPhase5ValidationError(
            f"{context} did not return JSON"
        ) from exc
    _assert(isinstance(body, dict), f"{context} returned an unexpected response shape")
    return body


def _authorization(
    access_token: str,
    request_id: str | None = None,
) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {access_token}"}
    if request_id:
        headers["X-Request-ID"] = request_id
    return headers


def _login(
    client: httpx.Client,
    email: str,
    password: str,
    env: dict[str, str],
) -> dict[str, Any]:
    body = _expect(
        client.post(
            "/auth/login",
            json={"username": email, "password": password},
        ),
        200,
        f"login {email}",
    )
    _assert(
        isinstance(body.get("access_token"), str),
        "login response is missing access_token",
    )
    _assert(
        isinstance(body.get("refresh_token"), str),
        "login response is missing refresh_token",
    )
    body["verified_claims"] = jwt.decode(
        str(body["access_token"]),
        env["JWT_PUBLIC_KEY"],
        algorithms=[env["JWT_ALGORITHM"]],
        issuer=env["JWT_ISSUER"],
        audience=env["JWT_AUDIENCE"],
    )
    return body


def _token_sid(login: dict[str, Any]) -> str:
    sid = login["verified_claims"].get("sid")
    _assert(isinstance(sid, str) and bool(sid), "issued access token is missing sid")
    return str(sid)


def _assert_access_and_refresh_rejected(
    client: httpx.Client,
    access_token: str,
    refresh_token: str,
) -> None:
    _expect(
        client.get("/auth/me", headers=_authorization(access_token)),
        401,
        "revoked access token check",
    )
    _expect(
        client.post(
            "/auth/refresh",
            json={"refresh_token": refresh_token},
        ),
        401,
        "revoked refresh token check",
    )


def _assert_no_sensitive_fields(payload: Any, context: str) -> None:
    serialized = json.dumps(payload, default=str).lower()
    for field in (
        "hashed_password",
        "access_token",
        "refresh_token",
        "session_id",
        "sid",
        "jti",
        "authorization",
    ):
        _assert(field not in serialized, f"{context} exposed {field}")


def _create_http_fixtures(database_url: str) -> dict[str, Any]:
    from app.core.security import hash_password
    from app.models.permission import Permission
    from app.models.role import Role, role_permissions, user_roles
    from app.models.user import User

    engine = create_engine(database_url, poolclass=NullPool)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    password = "permission-phase5-user-password"
    fixtures: dict[str, Any] = {"password": password, "principals": {}}
    try:
        with SessionLocal() as db:
            none_role = Role(name=f"permission_phase5_none_{uuid4().hex[:8]}")
            none_user = User(
                email=f"permission-phase5-none-{uuid4().hex[:8]}@example.com",
                hashed_password=hash_password(password),
                is_active=True,
                is_blacklisted=False,
            )
            db.add_all((none_role, none_user))
            db.flush()
            db.execute(
                user_roles.insert().values(
                    user_id=none_user.id,
                    role_id=none_role.id,
                )
            )
            fixtures["principals"]["none"] = {
                "email": none_user.email,
                "role": none_role.name,
                "scope": "",
            }

            for permission_name in PERMISSION_MANAGEMENT_PERMISSIONS:
                permission = db.scalar(
                    select(Permission).where(Permission.name == permission_name)
                )
                _assert(
                    permission is not None,
                    f"permission sync did not create {permission_name}",
                )
                slug = permission_name.replace(":", "_")
                role = Role(name=f"permission_phase5_{slug}_{uuid4().hex[:6]}")
                user = User(
                    email=f"permission-phase5-{slug}-{uuid4().hex[:8]}@example.com",
                    hashed_password=hash_password(password),
                    is_active=True,
                    is_blacklisted=False,
                )
                db.add_all((role, user))
                db.flush()
                db.execute(
                    role_permissions.insert().values(
                        role_id=role.id,
                        permission_id=permission.id,
                    )
                )
                db.execute(
                    user_roles.insert().values(user_id=user.id, role_id=role.id)
                )
                fixtures["principals"][permission_name] = {
                    "id": user.id,
                    "email": user.email,
                    "role": role.name,
                    "scope": permission_name,
                }

            target_permission = db.scalar(
                select(Permission).where(Permission.name == TARGET_PERMISSION)
            )
            _assert(target_permission is not None, "target permission is missing")
            target_role = Role(name=f"permission_phase5_target_{uuid4().hex[:8]}")
            target_user = User(
                email=f"permission-phase5-target-{uuid4().hex[:8]}@example.com",
                hashed_password=hash_password(password),
                is_active=True,
                is_blacklisted=False,
            )
            db.add_all((target_role, target_user))
            db.flush()
            db.execute(
                role_permissions.insert().values(
                    role_id=target_role.id,
                    permission_id=target_permission.id,
                )
            )
            db.execute(
                user_roles.insert().values(
                    user_id=target_user.id,
                    role_id=target_role.id,
                )
            )
            fixtures["target"] = {
                "id": target_user.id,
                "email": target_user.email,
                "role_id": target_role.id,
                "role": target_role.name,
                "permission_id": target_permission.id,
                "scope": target_permission.name,
            }
            db.commit()
    finally:
        engine.dispose()
    return fixtures


def _run_sync_catalog(
    database_url: str,
    scan_result: Any,
    *,
    redis_url: str,
    session_prefix: str,
    refresh_token_expire_days: int,
) -> dict[str, int]:
    from app.core.config import settings
    from app.core.redis import get_redis
    from app.services.permission_sync_service import PermissionSyncService

    engine = create_engine(database_url, poolclass=NullPool)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    original_redis_url = settings.redis_url
    original_session_prefix = settings.session_prefix
    original_refresh_days = settings.refresh_token_expire_days
    try:
        settings.redis_url = redis_url
        settings.session_prefix = session_prefix
        settings.refresh_token_expire_days = refresh_token_expire_days
        get_redis.cache_clear()
        with SessionLocal() as db:
            service = PermissionSyncService(db)
            summary = service.apply_plan(service.build_plan(scan_result))
            db.commit()
            return summary.to_dict()
    finally:
        settings.redis_url = original_redis_url
        settings.session_prefix = original_session_prefix
        settings.refresh_token_expire_days = original_refresh_days
        get_redis.cache_clear()
        engine.dispose()


def _run_http_permission_flow(
    client: httpx.Client,
    engine: Engine,
    redis_client: Redis,
    env: dict[str, str],
    fixtures: dict[str, Any],
) -> tuple[dict[str, Any], set[str]]:
    from app.main import create_app
    from app.services.permission_scanner import scan_permission_routes

    password = str(fixtures["password"])
    principals = fixtures["principals"]
    logins: dict[str, dict[str, Any]] = {}
    sensitive_values: set[str] = {password}
    for name, principal in principals.items():
        login = _login(client, principal["email"], password, env)
        claims = login["verified_claims"]
        _assert(
            claims["roles"] == [principal["role"]],
            f"{name} token has unexpected roles",
        )
        _assert(
            str(claims["scope"]) == principal["scope"],
            f"{name} token has unexpected scope",
        )
        logins[name] = login
        sensitive_values.update(
            (
                str(login["access_token"]),
                str(login["refresh_token"]),
                _token_sid(login),
            )
        )

    admin_login = _login(client, "admin@example.com", "password123", env)
    admin_claims = admin_login["verified_claims"]
    _assert(
        set(PERMISSION_MANAGEMENT_PERMISSIONS)
        <= set(str(admin_claims["scope"]).split()),
        "admin token is missing permission management permissions",
    )
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
    _assert(
        target_claims["roles"] == [target["role"]]
        and target_claims["scope"] == TARGET_PERMISSION,
        "target token does not contain the expected permission",
    )
    target_sid = _token_sid(target_login)
    sensitive_values.update(
        (
            str(target_login["access_token"]),
            str(target_login["refresh_token"]),
            target_sid,
        )
    )

    paths = _expect(client.get("/openapi.json"), 200, "OpenAPI document")["paths"]
    expected_paths = {
        "/admin/permissions": {"get"},
        "/admin/permissions/{permission_id}": {"get", "patch"},
        "/admin/permissions/{permission_id}/disable": {"post"},
        "/admin/permissions/{permission_id}/enable": {"post"},
    }
    for path, methods in expected_paths.items():
        _assert(
            path in paths and methods <= set(paths[path]),
            f"OpenAPI is missing {path} methods",
        )
        for method in methods:
            _assert(
                bool(paths[path][method].get("security")),
                f"OpenAPI {method.upper()} {path} has no security",
            )
    _assert(
        "post" not in paths["/admin/permissions"],
        "OpenAPI exposes unsupported permission creation",
    )
    _assert(
        "delete" not in paths["/admin/permissions/{permission_id}"],
        "OpenAPI exposes unsupported permission deletion",
    )

    _expect(client.get("/admin/permissions"), 401, "unauthenticated permission list")
    none_token = str(logins["none"]["access_token"])
    _expect(
        client.get("/admin/permissions", headers=_authorization(none_token)),
        403,
        "no-permission boundary",
    )
    with engine.connect() as connection:
        core_id = int(
            connection.execute(
                text("SELECT id FROM permissions WHERE name = 'permission:enable'")
            ).scalar_one()
        )

    permission_denials = 2
    permission_actions: dict[str, Callable[[str], httpx.Response]] = {
        "permission:read": lambda token: client.get(
            "/admin/permissions",
            headers=_authorization(token),
        ),
        "permission:update": lambda token: client.patch(
            f"/admin/permissions/{target['permission_id']}",
            headers=_authorization(token),
            json={"display_name": TARGET_PERMISSION, "version": 1},
        ),
        "permission:disable": lambda token: client.post(
            f"/admin/permissions/{target['permission_id']}/disable",
            headers=_authorization(token),
            json={"reason": "boundary"},
        ),
        "permission:enable": lambda token: client.post(
            f"/admin/permissions/{target['permission_id']}/enable",
            headers=_authorization(token),
        ),
    }
    for required_permission, action in permission_actions.items():
        wrong_permission = next(
            permission
            for permission in PERMISSION_MANAGEMENT_PERMISSIONS
            if permission != required_permission
        )
        _expect(
            action(str(logins[wrong_permission]["access_token"])),
            403,
            f"{wrong_permission} cannot use {required_permission} endpoint",
        )
        permission_denials += 1

    read_headers = _authorization(str(logins["permission:read"]["access_token"]))
    listed = _expect(
        client.get(
            "/admin/permissions",
            headers=read_headers,
            params={"resource": "app", "is_declared": True, "is_enabled": True},
        ),
        200,
        "list permissions",
    )
    _assert(
        listed["total"] >= 1
        and any(item["id"] == target["permission_id"] for item in listed["items"]),
        "permission list filtering failed",
    )
    detail = _expect(
        client.get(
            f"/admin/permissions/{target['permission_id']}",
            headers=read_headers,
        ),
        200,
        "permission details",
    )
    _assert(
        detail["name"] == TARGET_PERMISSION and detail["endpoint_count"] == 2,
        "permission detail or endpoint count is incorrect",
    )
    _assert(
        {(item["http_method"], item["path"]) for item in detail["endpoints"]}
        == {("GET", "/admin/apps"), ("GET", "/admin/apps/{app_id}")},
        "permission endpoint snapshot is incorrect",
    )
    _assert_no_sensitive_fields(detail, "permission details")
    initial_version = int(detail["version"])

    unchanged = _expect(
        client.patch(
            f"/admin/permissions/{target['permission_id']}",
            headers=_authorization(
                str(logins["permission:update"]["access_token"]),
                "permission-phase5-update-no-change",
            ),
            json={
                "display_name": detail["display_name"],
                "description": detail["description"],
                "version": initial_version,
            },
        ),
        200,
        "no-change permission update",
    )
    _assert(
        unchanged["changed"] is False and unchanged["version"] == initial_version,
        "no-change permission update changed state",
    )
    updated = _expect(
        client.patch(
            f"/admin/permissions/{target['permission_id']}",
            headers=_authorization(
                str(logins["permission:update"]["access_token"]),
                "permission-phase5-update",
            ),
            json={
                "display_name": "Phase five App reader",
                "description": "Permission phase five lifecycle target",
                "version": initial_version,
            },
        ),
        200,
        "update permission",
    )
    _assert(
        updated["changed"] is True and updated["version"] == initial_version + 1,
        "permission update failed",
    )
    stale = _expect(
        client.patch(
            f"/admin/permissions/{target['permission_id']}",
            headers=_authorization(str(logins["permission:update"]["access_token"])),
            json={"display_name": "Stale", "version": initial_version},
        ),
        409,
        "stale permission update",
    )
    _assert(
        stale.get("detail") == "PERMISSION_VERSION_CONFLICT",
        "stale permission update returned the wrong error",
    )
    missing = _expect(
        client.get("/admin/permissions/999999999", headers=read_headers),
        404,
        "missing permission",
    )
    _assert(
        missing.get("detail") == "PERMISSION_NOT_FOUND",
        "missing permission returned the wrong error",
    )
    invalid = client.patch(
        f"/admin/permissions/{target['permission_id']}",
        headers=_authorization(str(logins["permission:update"]["access_token"])),
        json={"display_name": "   ", "version": updated["version"]},
    )
    _expect(invalid, 422, "invalid permission update")

    protected = _expect(
        client.post(
            f"/admin/permissions/{core_id}/disable",
            headers=_authorization(str(logins["permission:disable"]["access_token"])),
            json={"reason": "forbidden"},
        ),
        409,
        "disable core enable permission",
    )
    _assert(
        protected.get("detail") == "PROTECTED_PERMISSION_OPERATION",
        "core permission protection returned the wrong error",
    )

    disabled = _expect(
        client.post(
            f"/admin/permissions/{target['permission_id']}/disable",
            headers=_authorization(
                str(logins["permission:disable"]["access_token"]),
                "permission-phase5-disable",
            ),
            json={"reason": "permission phase 5 maintenance"},
        ),
        200,
        "disable permission",
    )
    _assert(
        disabled["changed"] is True
        and disabled["is_enabled"] is False
        and disabled["version"] == updated["version"] + 1,
        "permission disable failed",
    )
    _assert(
        disabled["revoked_sessions"] == 2,
        "permission disable did not revoke the target and admin sessions",
    )
    for revoked_login in (target_login, admin_login):
        _assert_session_revoked(
            engine,
            redis_client,
            env["SESSION_PREFIX"],
            _token_sid(revoked_login),
            "permission_disabled",
            int(env["REFRESH_TOKEN_EXPIRE_DAYS"]) * 86_400,
        )
        _assert_access_and_refresh_rejected(
            client,
            str(revoked_login["access_token"]),
            str(revoked_login["refresh_token"]),
        )
    with engine.connect() as connection:
        preserved = connection.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM permission_endpoints WHERE permission_id = :id) AS endpoints, "
                "(SELECT count(*) FROM role_permissions WHERE permission_id = :id) AS role_links"
            ),
            {"id": target["permission_id"]},
        ).mappings().one()
    _assert(
        preserved["endpoints"] == 2 and preserved["role_links"] >= 2,
        "permission disable removed endpoint or role associations",
    )
    login_without_permission = _login(client, target["email"], password, env)
    _assert(
        login_without_permission["verified_claims"]["scope"] == "",
        "disabled permission entered a new JWT scope",
    )
    sensitive_values.update(
        (
            str(login_without_permission["access_token"]),
            str(login_without_permission["refresh_token"]),
            _token_sid(login_without_permission),
        )
    )
    disabled_again = _expect(
        client.post(
            f"/admin/permissions/{target['permission_id']}/disable",
            headers=_authorization(str(logins["permission:disable"]["access_token"])),
            json={"reason": "must not replace"},
        ),
        200,
        "repeat permission disable",
    )
    _assert(
        disabled_again["changed"] is False
        and disabled_again["disabled_reason"] == "permission phase 5 maintenance"
        and disabled_again["version"] == disabled["version"],
        "repeat permission disable was not idempotent",
    )

    enabled = _expect(
        client.post(
            f"/admin/permissions/{target['permission_id']}/enable",
            headers=_authorization(
                str(logins["permission:enable"]["access_token"]),
                "permission-phase5-enable",
            ),
        ),
        200,
        "enable permission",
    )
    _assert(
        enabled["changed"] is True
        and enabled["is_enabled"] is True
        and enabled["disabled_at"] is None
        and enabled["disabled_reason"] is None
        and enabled["version"] == disabled["version"] + 1,
        "permission enable failed",
    )
    enabled_again = _expect(
        client.post(
            f"/admin/permissions/{target['permission_id']}/enable",
            headers=_authorization(str(logins["permission:enable"]["access_token"])),
        ),
        200,
        "repeat permission enable",
    )
    _assert(
        enabled_again["changed"] is False
        and enabled_again["version"] == enabled["version"],
        "repeat permission enable was not idempotent",
    )
    _assert_access_and_refresh_rejected(
        client,
        str(target_login["access_token"]),
        str(target_login["refresh_token"]),
    )
    login_after_enable = _login(client, target["email"], password, env)
    _assert(
        login_after_enable["verified_claims"]["scope"] == TARGET_PERMISSION,
        "re-enabled permission did not enter the new JWT scope",
    )
    enabled_sid = _token_sid(login_after_enable)
    sensitive_values.update(
        (
            str(login_after_enable["access_token"]),
            str(login_after_enable["refresh_token"]),
            enabled_sid,
        )
    )

    full_catalog = scan_permission_routes(create_app())
    missing_catalog = _without_permission(full_catalog, TARGET_PERMISSION)
    missing_summary = _run_sync_catalog(
        env["DATABASE_URL"],
        missing_catalog,
        redis_url=env["REDIS_URL"],
        session_prefix=env["SESSION_PREFIX"],
        refresh_token_expire_days=int(env["REFRESH_TOKEN_EXPIRE_DAYS"]),
    )
    _assert(
        missing_summary["marked_missing"] == 1
        and missing_summary["endpoint_bindings_removed"] == 2
        and missing_summary["sessions_revoked"] == 2,
        f"permission missing synchronization summary is incorrect: {missing_summary}",
    )
    for label, revoked_login in (
        ("enabled", login_after_enable),
        ("disabled", login_without_permission),
    ):
        try:
            _assert_session_revoked(
                engine,
                redis_client,
                env["SESSION_PREFIX"],
                _token_sid(revoked_login),
                "permission_sync",
                int(env["REFRESH_TOKEN_EXPIRE_DAYS"]) * 86_400,
            )
        except PermissionPhase5ValidationError as exc:
            raise PermissionPhase5ValidationError(
                f"{label} target login revocation failed: {exc}"
            ) from exc
    _assert_access_and_refresh_rejected(
        client,
        str(login_after_enable["access_token"]),
        str(login_after_enable["refresh_token"]),
    )
    with engine.connect() as connection:
        missing_state = connection.execute(
            text(
                "SELECT id, display_name, is_declared, is_enabled, missing_at, version, "
                "(SELECT count(*) FROM permission_endpoints pe WHERE pe.permission_id = p.id) AS endpoints, "
                "(SELECT count(*) FROM role_permissions rp WHERE rp.permission_id = p.id) AS role_links "
                "FROM permissions p WHERE p.id = :id"
            ),
            {"id": target["permission_id"]},
        ).mappings().one()
    _assert(
        missing_state["id"] == target["permission_id"]
        and missing_state["display_name"] == "Phase five App reader"
        and missing_state["is_declared"] is False
        and missing_state["is_enabled"] is True
        and missing_state["missing_at"] is not None
        and missing_state["endpoints"] == 0
        and missing_state["role_links"] >= 2,
        "missing synchronization did not preserve permission identity and associations",
    )

    missing_enable = _expect(
        client.post(
            f"/admin/permissions/{target['permission_id']}/enable",
            headers=_authorization(str(logins["permission:enable"]["access_token"])),
        ),
        409,
        "enable missing permission",
    )
    _assert(
        missing_enable.get("detail") == "PERMISSION_NOT_DECLARED",
        "missing permission enable returned the wrong error",
    )

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE sessions SET status = 'active', revoked_at = NULL, "
                "revoked_reason = NULL WHERE sid = :sid"
            ),
            {"sid": enabled_sid},
        )
    redis_client.delete(f"{env['SESSION_PREFIX']}{enabled_sid}")
    stale_scope_denied = _expect(
        client.get(
            "/admin/apps",
            headers=_authorization(str(login_after_enable["access_token"])),
        ),
        403,
        "missing database permission with stale scope",
    )
    _assert(
        stale_scope_denied.get("detail") == "insufficient permissions",
        "database permission status check returned the wrong error",
    )

    restore_summary = _run_sync_catalog(
        env["DATABASE_URL"],
        full_catalog,
        redis_url=env["REDIS_URL"],
        session_prefix=env["SESSION_PREFIX"],
        refresh_token_expire_days=int(env["REFRESH_TOKEN_EXPIRE_DAYS"]),
    )
    _assert(
        restore_summary["restored"] == 1
        and restore_summary["endpoint_bindings_added"] == 2,
        "permission restore synchronization summary is incorrect",
    )
    with engine.connect() as connection:
        restored = connection.execute(
            text(
                "SELECT id, display_name, is_declared, is_enabled, missing_at, "
                "(SELECT count(*) FROM permission_endpoints pe WHERE pe.permission_id = p.id) AS endpoints, "
                "(SELECT count(*) FROM role_permissions rp WHERE rp.permission_id = p.id) AS role_links "
                "FROM permissions p WHERE p.id = :id"
            ),
            {"id": target["permission_id"]},
        ).mappings().one()
    _assert(
        restored["id"] == target["permission_id"]
        and restored["display_name"] == "Phase five App reader"
        and restored["is_declared"] is True
        and restored["is_enabled"] is True
        and restored["missing_at"] is None
        and restored["endpoints"] == 2
        and restored["role_links"] >= 2,
        "permission restoration did not preserve identity, metadata, or associations",
    )
    restored_login = _login(client, target["email"], password, env)
    _assert(
        restored_login["verified_claims"]["scope"] == TARGET_PERMISSION,
        "restored permission did not enter a new JWT scope",
    )
    sensitive_values.update(
        (
            str(restored_login["access_token"]),
            str(restored_login["refresh_token"]),
            _token_sid(restored_login),
        )
    )

    with engine.connect() as connection:
        audits = connection.execute(
            text(
                "SELECT actor_user_id, action, target_type, target_id, reason, "
                "changes_json, request_id FROM audit_events "
                "WHERE target_type = 'permission' AND target_id = :id ORDER BY id"
            ),
            {"id": target["permission_id"]},
        ).mappings().all()
        catalog_counts = connection.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM permissions) AS permissions, "
                "(SELECT count(*) FROM permission_endpoints) AS endpoints, "
                "(SELECT count(*) FROM role_permissions rp JOIN roles r ON r.id = rp.role_id "
                "WHERE r.name = 'admin') AS admin_grants"
            )
        ).mappings().one()
    _assert(
        [audit["action"] for audit in audits]
        == ["permission.updated", "permission.disabled", "permission.enabled"],
        "permission lifecycle audits are incorrect",
    )
    _assert(
        {
            "permission-phase5-update",
            "permission-phase5-disable",
            "permission-phase5-enable",
        }
        == {audit["request_id"] for audit in audits},
        "permission audit request IDs are incorrect",
    )
    _assert(
        audits[1]["reason"] == "permission phase 5 maintenance",
        "permission disable audit reason is incorrect",
    )
    _assert(
        audits[1]["changes_json"].get("revoked_sessions") == 2,
        "permission disable audit revocation count is incorrect",
    )
    _assert(
        catalog_counts["permissions"] == 25
        and catalog_counts["endpoints"] == 31
        and catalog_counts["admin_grants"] == 25,
        "final permission catalog counts are incorrect",
    )
    audit_text = json.dumps([dict(audit) for audit in audits], default=str)
    for secret in sensitive_values:
        _assert(secret not in audit_text, "permission audit exposed sensitive data")
    _assert_no_sensitive_fields(
        {"audits": [dict(audit) for audit in audits]},
        "permission audits",
    )

    return (
        {
            "permissions_verified": len(PERMISSION_MANAGEMENT_PERMISSIONS),
            "permission_denials_verified": permission_denials,
            "catalog_counts": [
                int(catalog_counts["permissions"]),
                int(catalog_counts["endpoints"]),
                int(catalog_counts["admin_grants"]),
            ],
            "lifecycle_audits_verified": len(audits),
            "request_ids_verified": 3,
            "redis_revocations_verified": 5,
            "old_sessions_rejected": True,
            "disabled_scope_filter": True,
            "missing_scope_filter": True,
            "database_status_denial": True,
            "reenabled_scope_restore": True,
            "missing_restore_identity_preserved": True,
            "seed_sync_idempotency_verified": True,
        },
        sensitive_values,
    )


def run_http_validation(config: PermissionPhase5Config) -> dict[str, Any]:
    with temporary_postgres_database(config, "http") as database:
        suffix = database.name.rsplit("_", 1)[-1]
        redis_prefix = f"auth:permission-phase5:{suffix}:"
        redis_client: Redis = Redis.from_url(config.redis_url, decode_responses=True)
        redis_client.ping()
        _clean_redis_namespace(redis_client, redis_prefix)
        env = _runtime_env(database.url, config.redis_url, redis_prefix)
        _alembic(database.url, "upgrade", "head")
        _seed(env)
        _seed(env)
        first_sync = _permission_sync(env)
        second_sync = _permission_sync(env)
        check_sync = _permission_sync(env, "--check")
        _assert(
            first_sync["created"] == 25
            and first_sync["endpoint_bindings_added"] == 31
            and first_sync["admin_grants_added"] == 25,
            "initial permission sync counts are incorrect",
        )
        _assert(
            sum(
                second_sync[key]
                for key in (
                    "created",
                    "restored",
                    "marked_missing",
                    "endpoint_bindings_added",
                    "endpoint_bindings_removed",
                    "admin_grants_added",
                    "sessions_revoked",
                )
            )
            == 0,
            "second permission sync was not idempotent",
        )
        _assert(
            check_sync["has_changes"] is False and check_sync["exit_code"] == 0,
            "permission sync check did not report a clean catalog",
        )
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
            database.url,
            make_url(database.url).password or "",
            config.redis_url,
            urlparse(config.redis_url).password or "",
        }
        flow_error: Exception | None = None
        try:
            _wait_for_api(base_url, process)
            with httpx.Client(base_url=base_url, timeout=20) as client:
                result, flow_secrets = _run_http_permission_flow(
                    client,
                    engine,
                    redis_client,
                    env,
                    fixtures,
                )
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
                _assert(
                    not secret or secret not in log_text,
                    "API logs exposed sensitive permission validation data",
                )
        else:
            safe_error = _redact(
                str(flow_error),
                [*sensitive_values, *_database_secrets(database.url)],
            )
            safe_log_tail = _redact(
                "\n".join(log_text.splitlines()[-30:]),
                list(sensitive_values),
            )
            raise PermissionPhase5ValidationError(
                "permission management HTTP validation failed: "
                f"{safe_error}\nAPI log tail:\n{safe_log_tail}"
            ) from flow_error

        _assert(result is not None, "permission HTTP validation produced no result")
        return {
            "database": database.name,
            **result,
            "real_rs256_permissions": True,
            "sensitive_log_scan": "clean",
            "temporary_resources_cleaned": True,
        }


def run_all_validations(
    config: PermissionPhase5Config,
) -> dict[str, dict[str, Any]]:
    return {
        "migration": run_migration_validation(config),
        "concurrency": run_concurrency_validation(config),
        "http": run_http_validation(config),
    }


def _print_report(name: str, report: dict[str, Any]) -> None:
    print(f"[PASS] {name}: {json.dumps(report, sort_keys=True)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate permission management phase 5 with isolated PostgreSQL and Redis"
        )
    )
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
        config = PermissionPhase5Config.from_env()
        if args.only in {"all", "migration"}:
            _print_report(
                "Permission Alembic migration roundtrip",
                run_migration_validation(config),
            )
        if args.only in {"all", "concurrency"}:
            _print_report(
                "Permission PostgreSQL concurrency and Redis rollback",
                run_concurrency_validation(config),
            )
        if args.only in {"all", "http"}:
            _print_report(
                "Permission JWT, Redis, sync, and HTTP lifecycle",
                run_http_validation(config),
            )
    except (OSError, ValueError, PermissionPhase5ValidationError) as exc:
        database_url = os.getenv("PERMISSION_PHASE5_ADMIN_DATABASE_URL", "")
        redis_url = os.getenv("PERMISSION_PHASE5_REDIS_URL", "")
        safe_error = _redact(
            str(exc),
            [
                *(_database_secrets(database_url) if database_url else ()),
                *(_redis_secrets(redis_url) if redis_url else ()),
            ],
        )
        print(f"[FAIL] permission phase 5 validation: {safe_error}", file=sys.stderr)
        return 1
    print("Permission phase 5 validation completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
