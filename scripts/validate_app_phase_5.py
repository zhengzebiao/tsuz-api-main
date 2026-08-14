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
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session as DbSession, sessionmaker
from sqlalchemy.pool import NullPool

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

DEFAULT_ADMIN_DATABASE_URL = "postgresql+psycopg://test_user:test_password@127.0.0.1:55432/postgres"
DEFAULT_REDIS_URL = "redis://127.0.0.1:56379/15"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
APP_PERMISSIONS = (
    "app:read",
    "app:create",
    "app:update",
    "app:enable",
    "app:disable",
    "app:regenerate_secret",
)
SENSITIVE_RESPONSE_FIELDS = {"app_secret", "app_secret_hash", "access_token", "refresh_token", "password"}


class AppPhase5ValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppPhase5Config:
    admin_database_url: str
    redis_url: str
    api_host: str
    api_port: int
    allow_remote: bool
    allow_default_ports: bool

    @classmethod
    def from_env(cls) -> AppPhase5Config:
        config = cls(
            admin_database_url=os.getenv("APP_PHASE5_ADMIN_DATABASE_URL", DEFAULT_ADMIN_DATABASE_URL),
            redis_url=os.getenv("APP_PHASE5_REDIS_URL", DEFAULT_REDIS_URL),
            api_host=os.getenv("APP_PHASE5_API_HOST", "127.0.0.1"),
            api_port=int(os.getenv("APP_PHASE5_API_PORT", "0")),
            allow_remote=os.getenv("APP_PHASE5_ALLOW_REMOTE", "0") == "1",
            allow_default_ports=os.getenv("APP_PHASE5_ALLOW_DEFAULT_PORTS", "0") == "1",
        )
        config.validate()
        return config

    def validate(self) -> None:
        database_url = make_url(self.admin_database_url)
        redis_url = urlparse(self.redis_url)
        if not database_url.drivername.startswith("postgresql"):
            raise AppPhase5ValidationError("APP_PHASE5_ADMIN_DATABASE_URL must use PostgreSQL")
        if redis_url.scheme not in {"redis", "rediss"}:
            raise AppPhase5ValidationError("APP_PHASE5_REDIS_URL must use redis:// or rediss://")
        if not 0 <= self.api_port <= 65535:
            raise AppPhase5ValidationError("APP_PHASE5_API_PORT must be between 0 and 65535")
        if not self.allow_remote:
            hosts = {database_url.host, redis_url.hostname, self.api_host}
            remote_hosts = sorted(host for host in hosts if host and host not in LOCAL_HOSTS)
            if remote_hosts:
                raise AppPhase5ValidationError(
                    "App phase 5 validation only allows local services by default; "
                    "set APP_PHASE5_ALLOW_REMOTE=1 for an explicitly approved isolated environment"
                )
        if not self.allow_default_ports:
            database_port = database_url.port or 5432
            redis_port = redis_url.port or 6379
            if database_port == 5432 or redis_port == 6379:
                raise AppPhase5ValidationError(
                    "App phase 5 validation refuses PostgreSQL 5432 or Redis 6379 by default; "
                    "use dedicated temporary ports or explicitly set APP_PHASE5_ALLOW_DEFAULT_PORTS=1"
                )


@dataclass(frozen=True)
class TemporaryDatabase:
    name: str
    url: str


T = TypeVar("T")


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AppPhase5ValidationError(message)


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
        raise AppPhase5ValidationError(f"command failed ({' '.join(command)}):\n{safe_output}")
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
        raise AppPhase5ValidationError("generated database name contains invalid characters")
    return f'"{identifier}"'


@contextmanager
def temporary_postgres_database(config: AppPhase5Config, purpose: str) -> Iterator[TemporaryDatabase]:
    suffix = uuid4().hex[:12]
    database_name = f"tsuz_app_phase5_{purpose}_{suffix}"
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
            "APP_ENV": "app-phase5",
            "DEBUG": "false",
            "LOG_LEVEL": "info",
            "LOG_FORMAT": "json",
            "DATABASE_URL": database_url,
            "REDIS_URL": redis_url,
            "REDIS_KEY_PREFIX": redis_prefix,
            "JWT_ALGORITHM": "RS256",
            "JWT_ISSUER": "auth-service-app-phase5",
            "JWT_AUDIENCE": "backend-api-app-phase5",
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


def run_migration_validation(config: AppPhase5Config) -> dict[str, Any]:
    with temporary_postgres_database(config, "migration") as database:
        _alembic(database.url, "upgrade", "0002_user_management")
        engine = create_engine(database.url, poolclass=NullPool)
        legacy_email = f"legacy-app-{uuid4().hex[:8]}@example.com"
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO users (email, hashed_password, is_active, is_blacklisted, version) "
                        "VALUES (:email, 'legacy-hash', true, false, 1)"
                    ),
                    {"email": legacy_email},
                )
        finally:
            engine.dispose()

        _alembic(database.url, "upgrade", "head")
        engine = create_engine(database.url, poolclass=NullPool)
        try:
            inspector = inspect(engine)
            columns = {column["name"]: column for column in inspector.get_columns("apps")}
            indexes = {index["name"]: index for index in inspector.get_indexes("apps")}
            expected_columns = {
                "id",
                "app_id",
                "app_secret_hash",
                "name",
                "icon_url",
                "access_url",
                "service_account_name",
                "is_enabled",
                "disabled_at",
                "disabled_reason",
                "secret_updated_at",
                "created_at",
                "updated_at",
                "version",
            }
            _assert(set(columns) == expected_columns, "apps table columns do not match the model")
            for column_name in {
                "id",
                "app_id",
                "app_secret_hash",
                "name",
                "access_url",
                "service_account_name",
                "is_enabled",
                "secret_updated_at",
                "created_at",
                "updated_at",
                "version",
            }:
                _assert(columns[column_name]["nullable"] is False, f"{column_name} must be NOT NULL")
            for column_name in {"icon_url", "disabled_at", "disabled_reason"}:
                _assert(columns[column_name]["nullable"] is True, f"{column_name} must be nullable")
            _assert(
                {"ix_apps_id", "ix_apps_app_id", "ix_apps_name", "ix_apps_is_enabled"} <= set(indexes),
                "apps indexes are incomplete",
            )
            _assert(indexes["ix_apps_app_id"]["unique"] is True, "app_id index is not unique")
            with engine.begin() as connection:
                inserted = connection.execute(
                    text(
                        "INSERT INTO apps (app_id, app_secret_hash, name, access_url, service_account_name) "
                        "VALUES (:app_id, :secret_hash, 'Defaults', 'https://defaults.example.com', 'Defaults Service') "
                        "RETURNING is_enabled, version, secret_updated_at, created_at, updated_at"
                    ),
                    {
                        "app_id": "app_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "secret_hash": "a" * 64,
                    },
                ).mappings().one()
                legacy_count = connection.execute(
                    text("SELECT count(*) FROM users WHERE email = :email"),
                    {"email": legacy_email},
                ).scalar_one()
            _assert(inserted["is_enabled"] is True, "database default is_enabled is not true")
            _assert(inserted["version"] == 1, "database default version is not 1")
            _assert(
                all(inserted[field] is not None for field in ("secret_updated_at", "created_at", "updated_at")),
                "database timestamp defaults are missing",
            )
            _assert(legacy_count == 1, "legacy user was lost during upgrade")
        finally:
            engine.dispose()

        check_output = _alembic(database.url, "check")
        _assert("No new upgrade operations detected" in check_output, "Alembic metadata check is not clean")
        _alembic(database.url, "downgrade", "0002_user_management")
        engine = create_engine(database.url, poolclass=NullPool)
        try:
            inspector = inspect(engine)
            _assert("apps" not in inspector.get_table_names(), "apps table survived downgrade")
            with engine.connect() as connection:
                legacy_count = connection.execute(
                    text("SELECT count(*) FROM users WHERE email = :email"),
                    {"email": legacy_email},
                ).scalar_one()
            _assert(legacy_count == 1, "legacy user was lost during downgrade")
        finally:
            engine.dispose()

        _alembic(database.url, "upgrade", "head")
        current_output = _alembic(database.url, "current")
        _assert("0006_email_registration" in current_output, "database did not return to App migration head")
        return {
            "database": database.name,
            "current_revision": "0006_email_registration",
            "alembic_check": "clean",
            "legacy_user_preserved": True,
            "app_columns_verified": 14,
            "app_indexes_verified": 4,
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
            _assert(release_first.wait(timeout=10), "timed out waiting to release first transaction")
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


def run_concurrency_validation(config: AppPhase5Config) -> dict[str, Any]:
    with temporary_postgres_database(config, "concurrency") as database:
        _alembic(database.url, "upgrade", "head")
        engine = create_engine(database.url, poolclass=NullPool)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        try:
            from app.core.security import verify_app_secret
            from app.models.app import App
            from app.models.audit_event import AuditEvent
            from app.models.user import User
            from app.schemas.admin_app import AdminAppCreate, AdminAppUpdate
            from app.services.admin_app_service import AdminAppService, AppVersionConflictError

            with SessionLocal() as db:
                actor = User(
                    email=f"app-concurrency-{uuid4().hex[:8]}@example.com",
                    hashed_password="not-used",
                    is_active=True,
                    is_blacklisted=False,
                )
                db.add(actor)
                db.flush()
                actor_id = actor.id
                app, initial_secret = AdminAppService(db).create_app(
                    AdminAppCreate(
                        name="Concurrent App",
                        icon_url=None,
                        access_url="https://concurrent.example.com",
                        service_account_name="Concurrent Service",
                    ),
                    actor_user_id=actor_id,
                    request_id="phase5-concurrency-create",
                )
                app_id = app.id
                db.commit()

            disable_first, disable_second, disable_waited = _run_locked_pair(
                engine,
                lambda db: AdminAppService(db).disable_app(
                    app_id,
                    actor_user_id=actor_id,
                    reason="first reason",
                    request_id="phase5-disable-first",
                ),
                lambda db: AdminAppService(db).disable_app(
                    app_id,
                    actor_user_id=actor_id,
                    reason="second reason",
                    request_id="phase5-disable-second",
                ),
            )
            _assert(disable_waited, "concurrent disable did not wait on a PostgreSQL row lock")
            _assert(disable_first[1] is True and disable_second[1] is False, "disable idempotency failed")
            with SessionLocal() as db:
                disabled = db.get(App, app_id)
                _assert(disabled is not None, "disabled App disappeared")
                _assert(disabled.version == 2, "concurrent disable incremented version more than once")
                _assert(disabled.disabled_reason == "first reason", "concurrent disable replaced the first reason")
                disabled_audits = db.scalars(
                    select(AuditEvent).where(
                        AuditEvent.target_type == "app",
                        AuditEvent.target_id == app_id,
                        AuditEvent.action == "app.disabled",
                    )
                ).all()
                _assert(len(disabled_audits) == 1, "concurrent disable created duplicate audits")
                AdminAppService(db).enable_app(app_id, actor_user_id=actor_id, request_id="phase5-reenable")
                db.commit()

            secret_first, secret_second, secret_waited = _run_locked_pair(
                engine,
                lambda db: AdminAppService(db).regenerate_secret(
                    app_id,
                    actor_user_id=actor_id,
                    reason="first rotation",
                    request_id="phase5-secret-first",
                ),
                lambda db: AdminAppService(db).regenerate_secret(
                    app_id,
                    actor_user_id=actor_id,
                    reason="second rotation",
                    request_id="phase5-secret-second",
                ),
            )
            _assert(secret_waited, "concurrent Secret rotation did not wait on a PostgreSQL row lock")
            first_secret = secret_first[1]
            second_secret = secret_second[1]
            _assert(first_secret != second_secret, "concurrent rotations returned the same Secret")
            with SessionLocal() as db:
                rotated = db.get(App, app_id)
                _assert(rotated is not None, "rotated App disappeared")
                _assert(rotated.version == 5, "concurrent rotations produced the wrong version")
                _assert(not verify_app_secret(initial_secret, rotated.app_secret_hash), "initial Secret remained active")
                _assert(not verify_app_secret(first_secret, rotated.app_secret_hash), "first concurrent Secret remained active")
                _assert(verify_app_secret(second_secret, rotated.app_secret_hash), "last concurrent Secret is not active")
                secret_audits = db.scalars(
                    select(AuditEvent).where(
                        AuditEvent.target_type == "app",
                        AuditEvent.target_id == app_id,
                        AuditEvent.action == "app.secret_regenerated",
                    )
                ).all()
                _assert(len(secret_audits) == 2, "concurrent rotations did not create two audits")
                serialized = json.dumps(
                    [
                        {"reason": audit.reason, "changes": audit.changes_json}
                        for audit in secret_audits
                    ],
                    default=str,
                )
                for secret in (initial_secret, first_secret, second_secret, rotated.app_secret_hash):
                    _assert(secret not in serialized, "Secret rotation audit exposed a credential")

            with SessionLocal() as first_db, SessionLocal() as stale_db:
                current = first_db.get(App, app_id)
                stale = stale_db.get(App, app_id)
                _assert(current is not None and stale is not None, "App missing before optimistic lock validation")
                version = current.version
                updated, changed = AdminAppService(first_db).update_app(
                    app_id,
                    AdminAppUpdate(name="Optimistic Winner", version=version),
                    actor_user_id=actor_id,
                    request_id="phase5-update-winner",
                )
                _assert(changed and updated.version == version + 1, "winning optimistic update failed")
                first_db.commit()
                conflict_verified = False
                try:
                    AdminAppService(stale_db).update_app(
                        app_id,
                        AdminAppUpdate(name="Stale Loser", version=version),
                        actor_user_id=actor_id,
                        request_id="phase5-update-loser",
                    )
                except AppVersionConflictError:
                    stale_db.rollback()
                    conflict_verified = True
                _assert(conflict_verified, "stale optimistic update did not conflict")

            with SessionLocal() as db:
                final_app = db.get(App, app_id)
                _assert(final_app is not None and final_app.name == "Optimistic Winner", "stale update overwrote data")
            return {
                "database": database.name,
                "row_lock_waits_verified": 2,
                "disable_changes": [disable_first[1], disable_second[1]],
                "secret_rotations_serialized": 2,
                "optimistic_conflict": "APP_VERSION_CONFLICT",
                "temporary_resources_cleaned": True,
            }
        finally:
            engine.dispose()


def _find_available_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _wait_for_api(base_url: str, process: subprocess.Popen[str], timeout_seconds: float = 30) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AppPhase5ValidationError("App phase 5 API process exited before becoming healthy")
        try:
            if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise AppPhase5ValidationError("timed out waiting for the App phase 5 API process")


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
        raise AppPhase5ValidationError(
            f"{context} returned {response.status_code}, expected {expected_status}: {_safe_response_body(response)}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise AppPhase5ValidationError(f"{context} did not return JSON") from exc
    _assert(isinstance(body, dict), f"{context} returned an unexpected response shape")
    return body


def _authorization(access_token: str, request_id: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {access_token}"}
    if request_id:
        headers["X-Request-ID"] = request_id
    return headers


def _login(client: httpx.Client, email: str, password: str) -> dict[str, Any]:
    body = _expect(client.post("/auth/login", json={"username": email, "password": password}), 200, f"login {email}")
    _assert(isinstance(body.get("access_token"), str), "login response is missing access_token")
    _assert(isinstance(body.get("refresh_token"), str), "login response is missing refresh_token")
    return body


def _assert_no_credentials(payload: Any, context: str) -> None:
    serialized = json.dumps(payload, default=str)
    _assert("app_secret" not in serialized, f"{context} exposed app_secret")
    _assert("app_secret_hash" not in serialized, f"{context} exposed app_secret_hash")


def _create_permission_users(database_url: str) -> dict[str, dict[str, str]]:
    from app.core.security import hash_password
    from app.models.permission import Permission
    from app.models.role import Role, role_permissions, user_roles
    from app.models.user import User

    engine = create_engine(database_url, poolclass=NullPool)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    password = "app-phase5-permission-password"
    users: dict[str, dict[str, str]] = {}
    try:
        with SessionLocal() as db:
            no_app_permission = Permission(name="phase5:unrelated", description="Unrelated phase 5 permission")
            no_app_role = Role(name=f"app_phase5_none_{uuid4().hex[:8]}")
            no_app_user = User(
                email=f"app-phase5-none-{uuid4().hex[:8]}@example.com",
                hashed_password=hash_password(password),
                is_active=True,
                is_blacklisted=False,
            )
            db.add_all((no_app_permission, no_app_role, no_app_user))
            db.flush()
            db.execute(
                role_permissions.insert().values(
                    role_id=no_app_role.id,
                    permission_id=no_app_permission.id,
                )
            )
            db.execute(user_roles.insert().values(user_id=no_app_user.id, role_id=no_app_role.id))
            users["none"] = {"email": no_app_user.email, "password": password, "permission": "phase5:unrelated"}

            for permission_name in APP_PERMISSIONS:
                permission = db.scalar(select(Permission).where(Permission.name == permission_name))
                _assert(permission is not None, f"permission sync did not create {permission_name}")
                slug = permission_name.replace(":", "_")
                role = Role(name=f"app_phase5_{slug}_{uuid4().hex[:6]}")
                user = User(
                    email=f"app-phase5-{slug}-{uuid4().hex[:8]}@example.com",
                    hashed_password=hash_password(password),
                    is_active=True,
                    is_blacklisted=False,
                )
                db.add_all((role, user))
                db.flush()
                db.execute(role_permissions.insert().values(role_id=role.id, permission_id=permission.id))
                db.execute(user_roles.insert().values(user_id=user.id, role_id=role.id))
                users[permission_name] = {
                    "email": user.email,
                    "password": password,
                    "permission": permission_name,
                }
            db.commit()
    finally:
        engine.dispose()
    return users


def _run_http_app_flow(
    client: httpx.Client,
    engine: Engine,
    permission_users: dict[str, dict[str, str]],
) -> tuple[dict[str, Any], set[str]]:
    from app.core.security import verify_app_secret

    admin_login = _login(client, "admin@example.com", "password123")
    admin_token = str(admin_login["access_token"])
    none_login = _login(client, permission_users["none"]["email"], permission_users["none"]["password"])
    none_token = str(none_login["access_token"])
    permission_tokens: dict[str, str] = {}
    sensitive_values = {
        str(admin_login["access_token"]),
        str(admin_login["refresh_token"]),
        str(none_login["access_token"]),
        str(none_login["refresh_token"]),
        permission_users["none"]["password"],
    }
    for permission_name in APP_PERMISSIONS:
        credentials = permission_users[permission_name]
        login = _login(client, credentials["email"], credentials["password"])
        access_token = str(login["access_token"])
        payload = jwt.decode(access_token, options={"verify_signature": False})
        scope = set(str(payload.get("scope", "")).split())
        _assert(scope == {permission_name}, f"{permission_name} token has unexpected scope")
        permission_tokens[permission_name] = access_token
        sensitive_values.update((access_token, str(login["refresh_token"])))

    _expect(client.get("/admin/apps"), 401, "unauthenticated App list")
    _expect(client.get("/admin/apps", headers=_authorization(none_token)), 403, "no-App-permission boundary")

    create_payload = {
        "name": "Phase Five Project",
        "icon_url": "https://static.example.com/app-phase5.png",
        "access_url": "https://app-phase5.example.com",
        "service_account_name": "Phase Five Service",
    }
    created_response = client.post(
        "/admin/apps",
        headers=_authorization(permission_tokens["app:create"], "app-phase5-create"),
        json=create_payload,
    )
    created = _expect(created_response, 201, "create App with app:create")
    _assert(created_response.headers.get("Cache-Control") == "no-store", "create response is cacheable")
    app = created["app"]
    app_id = int(app["id"])
    app_secret = str(created["app_secret"])
    sensitive_values.add(app_secret)
    _assert(app_secret.startswith("app_secret_"), "create response did not return an App Secret")
    _assert_no_credentials(app, "create App body")

    for permission_name in ("app:read", "app:update", "app:enable", "app:disable", "app:regenerate_secret"):
        response = client.post(
            "/admin/apps",
            headers=_authorization(permission_tokens[permission_name]),
            json=create_payload,
        )
        _expect(response, 403, f"{permission_name} cannot create App")

    listed = _expect(
        client.get(
            "/admin/apps",
            headers=_authorization(permission_tokens["app:read"]),
            params={"keyword": app["app_id"], "is_enabled": True},
        ),
        200,
        "list App with app:read",
    )
    _assert(listed["total"] == 1 and listed["items"][0]["id"] == app_id, "App list filtering failed")
    _assert_no_credentials(listed, "App list")
    detail = _expect(
        client.get(f"/admin/apps/{app_id}", headers=_authorization(permission_tokens["app:read"])),
        200,
        "get App with app:read",
    )
    _assert_no_credentials(detail, "App details")

    no_change = _expect(
        client.patch(
            f"/admin/apps/{app_id}",
            headers=_authorization(permission_tokens["app:update"], "app-phase5-update-no-change"),
            json={"name": app["name"], "version": app["version"]},
        ),
        200,
        "no-change App update",
    )
    _assert(no_change["changed"] is False and no_change["version"] == 1, "no-change update changed App")
    updated = _expect(
        client.patch(
            f"/admin/apps/{app_id}",
            headers=_authorization(permission_tokens["app:update"], "app-phase5-update"),
            json={"name": "Phase Five Project Updated", "version": 1},
        ),
        200,
        "update App with app:update",
    )
    _assert(updated["changed"] is True and updated["version"] == 2, "App update failed")
    conflict = _expect(
        client.patch(
            f"/admin/apps/{app_id}",
            headers=_authorization(permission_tokens["app:update"]),
            json={"name": "Stale", "version": 1},
        ),
        409,
        "stale App update",
    )
    _assert(conflict.get("detail") == "APP_VERSION_CONFLICT", "stale update returned the wrong error")

    disabled = _expect(
        client.post(
            f"/admin/apps/{app_id}/disable",
            headers=_authorization(permission_tokens["app:disable"], "app-phase5-disable"),
            json={"reason": "phase 5 maintenance"},
        ),
        200,
        "disable App with app:disable",
    )
    _assert(disabled["changed"] is True and disabled["version"] == 3, "App disable failed")
    disabled_at = disabled["disabled_at"]
    disabled_again = _expect(
        client.post(
            f"/admin/apps/{app_id}/disable",
            headers=_authorization(permission_tokens["app:disable"]),
            json={"reason": "must not replace"},
        ),
        200,
        "repeat App disable",
    )
    _assert(disabled_again["changed"] is False and disabled_again["version"] == 3, "repeat disable changed App")
    _assert(
        disabled_again["disabled_at"] == disabled_at and disabled_again["disabled_reason"] == "phase 5 maintenance",
        "repeat disable replaced metadata",
    )

    enabled = _expect(
        client.post(
            f"/admin/apps/{app_id}/enable",
            headers=_authorization(permission_tokens["app:enable"], "app-phase5-enable"),
        ),
        200,
        "enable App with app:enable",
    )
    _assert(enabled["changed"] is True and enabled["version"] == 4, "App enable failed")
    enabled_again = _expect(
        client.post(f"/admin/apps/{app_id}/enable", headers=_authorization(permission_tokens["app:enable"])),
        200,
        "repeat App enable",
    )
    _assert(enabled_again["changed"] is False and enabled_again["version"] == 4, "repeat enable changed App")

    regenerated_response = client.post(
        f"/admin/apps/{app_id}/regenerate-secret",
        headers=_authorization(permission_tokens["app:regenerate_secret"], "app-phase5-secret"),
        json={"reason": "phase 5 rotation"},
    )
    regenerated = _expect(regenerated_response, 200, "regenerate App Secret")
    _assert(regenerated_response.headers.get("Cache-Control") == "no-store", "Secret response is cacheable")
    new_secret = str(regenerated["app_secret"])
    sensitive_values.add(new_secret)
    _assert(new_secret != app_secret, "Secret rotation returned the old Secret")

    detail_after = _expect(
        client.get(f"/admin/apps/{app_id}", headers=_authorization(permission_tokens["app:read"])),
        200,
        "get App after Secret rotation",
    )
    _assert(detail_after["version"] == 5, "Secret rotation did not increment version")
    _assert_no_credentials(detail_after, "App details after rotation")

    permission_actions = {
        "app:read": lambda token: client.get("/admin/apps", headers=_authorization(token)),
        "app:create": lambda token: client.post("/admin/apps", headers=_authorization(token), json=create_payload),
        "app:update": lambda token: client.patch(
            f"/admin/apps/{app_id}",
            headers=_authorization(token),
            json={"name": detail_after["name"], "version": detail_after["version"]},
        ),
        "app:disable": lambda token: client.post(
            f"/admin/apps/{app_id}/disable",
            headers=_authorization(token),
            json={"reason": "boundary"},
        ),
        "app:enable": lambda token: client.post(f"/admin/apps/{app_id}/enable", headers=_authorization(token)),
        "app:regenerate_secret": lambda token: client.post(
            f"/admin/apps/{app_id}/regenerate-secret",
            headers=_authorization(token),
            json={"reason": "boundary"},
        ),
    }
    permission_denials = 0
    for required_permission, action in permission_actions.items():
        wrong_permission = next(permission for permission in APP_PERMISSIONS if permission != required_permission)
        response = action(permission_tokens[wrong_permission])
        _expect(response, 403, f"{wrong_permission} cannot use {required_permission} endpoint")
        permission_denials += 1

    with engine.connect() as connection:
        stored = connection.execute(
            text("SELECT app_secret_hash, version FROM apps WHERE id = :app_id"),
            {"app_id": app_id},
        ).mappings().one()
        audits = connection.execute(
            text(
                "SELECT actor_user_id, action, reason, changes_json, request_id "
                "FROM audit_events WHERE target_type = 'app' AND target_id = :app_id ORDER BY id"
            ),
            {"app_id": app_id},
        ).mappings().all()
        admin_id = connection.execute(
            text("SELECT id FROM users WHERE email = 'admin@example.com'")
        ).scalar_one()
    app_secret_hash = str(stored["app_secret_hash"])
    sensitive_values.add(app_secret_hash)
    _assert(stored["version"] == 5, "stored App version is incorrect")
    _assert(not verify_app_secret(app_secret, app_secret_hash), "old App Secret remained valid")
    _assert(verify_app_secret(new_secret, app_secret_hash), "new App Secret is not valid")
    expected_actions = [
        "app.created",
        "app.updated",
        "app.disabled",
        "app.enabled",
        "app.secret_regenerated",
    ]
    _assert([audit["action"] for audit in audits] == expected_actions, "App audit actions are incorrect")
    expected_request_ids = {
        "app-phase5-create",
        "app-phase5-update",
        "app-phase5-disable",
        "app-phase5-enable",
        "app-phase5-secret",
    }
    _assert(expected_request_ids == {audit["request_id"] for audit in audits}, "App audit request IDs are incorrect")
    _assert(
        audits[0]["actor_user_id"] != admin_id,
        "single-permission App actions unexpectedly used the admin actor",
    )
    _assert(
        len({audit["actor_user_id"] for audit in audits}) == len(audits),
        "App audit actors do not match the distinct permission principals",
    )
    _assert(audits[-1]["changes_json"] == {"secret_changed": True}, "Secret audit changes are unsafe")
    _assert(audits[-1]["reason"] == "phase 5 rotation", "Secret audit reason is incorrect")
    audit_text = json.dumps([dict(audit) for audit in audits], default=str)
    for secret in sensitive_values:
        _assert(secret not in audit_text, "App audit exposed sensitive data")

    admin_scope = set(
        str(jwt.decode(admin_token, options={"verify_signature": False}).get("scope", "")).split()
    )
    _assert(set(APP_PERMISSIONS) <= admin_scope, "admin Token is missing App permissions")
    return (
        {
            "app_id": app_id,
            "permissions_verified": len(APP_PERMISSIONS),
            "permission_denials_verified": permission_denials + 2,
            "lifecycle_audits_verified": len(audits),
            "request_ids_verified": len(expected_request_ids),
            "old_secret_invalidated": True,
            "one_time_secret_responses": 2,
        },
        sensitive_values,
    )


def run_http_validation(config: AppPhase5Config) -> dict[str, Any]:
    with temporary_postgres_database(config, "http") as database:
        suffix = database.name.rsplit("_", 1)[-1]
        redis_prefix = f"auth:app-phase5:{suffix}:"
        redis_client: Redis = Redis.from_url(config.redis_url, decode_responses=True)
        redis_client.ping()
        _clean_redis_namespace(redis_client, redis_prefix)
        env = _runtime_env(database.url, config.redis_url, redis_prefix)
        _alembic(database.url, "upgrade", "head")
        _seed(env)
        _seed(env)
        _sync_permissions(env)
        permission_users = _create_permission_users(database.url)
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
        sensitive_values: set[str] = {env["JWT_PRIVATE_KEY"], env["JWT_PUBLIC_KEY"], "password123"}
        flow_error: Exception | None = None
        try:
            _wait_for_api(base_url, process)
            with httpx.Client(base_url=base_url, timeout=15) as client:
                result, flow_secrets = _run_http_app_flow(client, engine, permission_users)
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
                _assert(secret not in log_text, "API logs exposed sensitive App validation data")
        else:
            safe_error = _redact(str(flow_error), [*sensitive_values, *_database_secrets(database.url)])
            safe_log_tail = _redact("\n".join(log_text.splitlines()[-30:]), list(sensitive_values))
            raise AppPhase5ValidationError(
                f"App management HTTP validation failed: {safe_error}\nAPI log tail:\n{safe_log_tail}"
            ) from flow_error

        _assert(result is not None, "App HTTP validation produced no result")
        return {
            "database": database.name,
            **result,
            "real_jwt_permissions": True,
            "sensitive_log_scan": "clean",
            "temporary_resources_cleaned": True,
        }


def run_all_validations(config: AppPhase5Config) -> dict[str, dict[str, Any]]:
    return {
        "migration": run_migration_validation(config),
        "concurrency": run_concurrency_validation(config),
        "http": run_http_validation(config),
    }


def _print_report(name: str, report: dict[str, Any]) -> None:
    print(f"[PASS] {name}: {json.dumps(report, sort_keys=True)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate App management phase 5 with isolated PostgreSQL and Redis")
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
        config = AppPhase5Config.from_env()
        if args.only in {"all", "migration"}:
            _print_report("App Alembic migration roundtrip", run_migration_validation(config))
        if args.only in {"all", "concurrency"}:
            _print_report("App PostgreSQL concurrency", run_concurrency_validation(config))
        if args.only in {"all", "http"}:
            _print_report("App JWT permission and lifecycle", run_http_validation(config))
    except (OSError, ValueError, AppPhase5ValidationError) as exc:
        database_url = os.getenv("APP_PHASE5_ADMIN_DATABASE_URL", "")
        safe_error = _redact(str(exc), _database_secrets(database_url) if database_url else ())
        print(f"[FAIL] App phase 5 validation: {safe_error}", file=sys.stderr)
        return 1
    print("App phase 5 validation completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
