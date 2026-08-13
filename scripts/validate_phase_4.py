from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from redis import Redis
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ADMIN_DATABASE_URL = "postgresql+psycopg://test_user:test_password@127.0.0.1:5432/postgres"
DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/15"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


class Phase4ValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Phase4Config:
    admin_database_url: str
    redis_url: str
    api_host: str
    api_port: int
    allow_remote: bool

    @classmethod
    def from_env(cls) -> Phase4Config:
        admin_database_url = os.getenv("PHASE4_ADMIN_DATABASE_URL", DEFAULT_ADMIN_DATABASE_URL)
        redis_url = os.getenv("PHASE4_REDIS_URL", DEFAULT_REDIS_URL)
        api_host = os.getenv("PHASE4_API_HOST", "127.0.0.1")
        api_port = int(os.getenv("PHASE4_API_PORT", "0"))
        allow_remote = os.getenv("PHASE4_ALLOW_REMOTE", "0") == "1"
        config = cls(
            admin_database_url=admin_database_url,
            redis_url=redis_url,
            api_host=api_host,
            api_port=api_port,
            allow_remote=allow_remote,
        )
        config.validate()
        return config

    def validate(self) -> None:
        database_url = make_url(self.admin_database_url)
        if not database_url.drivername.startswith("postgresql"):
            raise Phase4ValidationError("PHASE4_ADMIN_DATABASE_URL must use PostgreSQL")
        redis_url = urlparse(self.redis_url)
        if redis_url.scheme not in {"redis", "rediss"}:
            raise Phase4ValidationError("PHASE4_REDIS_URL must use redis:// or rediss://")
        if not 0 <= self.api_port <= 65535:
            raise Phase4ValidationError("PHASE4_API_PORT must be between 0 and 65535")
        if not self.allow_remote:
            hosts = {database_url.host, redis_url.hostname, self.api_host}
            remote_hosts = sorted(host for host in hosts if host and host not in LOCAL_HOSTS)
            if remote_hosts:
                raise Phase4ValidationError(
                    "phase 4 validation only allows local services by default; "
                    "set PHASE4_ALLOW_REMOTE=1 for an explicitly approved isolated test environment"
                )


@dataclass(frozen=True)
class TemporaryDatabase:
    name: str
    url: str


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise Phase4ValidationError(message)


def _redact(value: str, secrets: Sequence[str] = ()) -> str:
    redacted = value
    for secret in secrets:
        if secret:
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
        raise Phase4ValidationError(f"command failed ({' '.join(command)}):\n{safe_output}")
    return output


def _runtime_env(database_url: str, redis_url: str, redis_prefix: str) -> dict[str, str]:
    private_key, public_key = _generate_jwt_keys()
    env = os.environ.copy()
    env.update(
        {
            "APP_ENV": "phase4",
            "DEBUG": "false",
            "DATABASE_URL": database_url,
            "REDIS_URL": redis_url,
            "REDIS_KEY_PREFIX": redis_prefix,
            "JWT_ALGORITHM": "RS256",
            "JWT_ISSUER": "auth-service-phase4",
            "JWT_AUDIENCE": "backend-api-phase4",
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


def _alembic(database_url: str, *arguments: str) -> str:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    return _run_command(
        (sys.executable, "-m", "alembic", *arguments),
        env=env,
        secrets=_database_secrets(database_url),
    )


def _seed(env: dict[str, str]) -> None:
    _run_command(
        (sys.executable, "-m", "app.seed"),
        env=env,
        secrets=_database_secrets(env["DATABASE_URL"]),
    )


def _database_url_for(admin_database_url: str, database_name: str) -> str:
    url = make_url(admin_database_url).set(database=database_name)
    return url.render_as_string(hide_password=False)


def _quoted_identifier(identifier: str) -> str:
    if not identifier.replace("_", "").isalnum():
        raise Phase4ValidationError("generated database name contains invalid characters")
    return f'"{identifier}"'


@contextmanager
def temporary_postgres_database(config: Phase4Config, purpose: str) -> Iterator[TemporaryDatabase]:
    suffix = uuid4().hex[:12]
    database_name = f"tsuz_phase4_{purpose}_{suffix}"
    admin_engine = create_engine(
        config.admin_database_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    try:
        with admin_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            connection.exec_driver_sql(f"CREATE DATABASE {_quoted_identifier(database_name)}")
        yield TemporaryDatabase(
            name=database_name,
            url=_database_url_for(config.admin_database_url, database_name),
        )
    finally:
        try:
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


def run_migration_roundtrip(config: Phase4Config) -> dict[str, Any]:
    with temporary_postgres_database(config, "migration") as database:
        _alembic(database.url, "upgrade", "0001_initial_auth_schema")
        engine = create_engine(database.url, poolclass=NullPool)
        legacy_email = f"legacy-{uuid4().hex[:8]}@example.com"
        legacy_sid = f"legacy-{uuid4().hex}"
        try:
            with engine.begin() as connection:
                user_id = connection.execute(
                    text(
                        "INSERT INTO users (email, hashed_password, is_active) "
                        "VALUES (:email, :hashed_password, true) RETURNING id"
                    ),
                    {"email": legacy_email, "hashed_password": "legacy-password-hash"},
                ).scalar_one()
                connection.execute(
                    text(
                        "INSERT INTO sessions (sid, user_id, status, created_at) "
                        "VALUES (:sid, :user_id, 'active', CURRENT_TIMESTAMP)"
                    ),
                    {"sid": legacy_sid, "user_id": user_id},
                )
        finally:
            engine.dispose()

        _alembic(database.url, "upgrade", "0002_user_management")
        engine = create_engine(database.url, poolclass=NullPool)
        try:
            inspector = inspect(engine)
            tables = set(inspector.get_table_names())
            user_columns = {column["name"] for column in inspector.get_columns("users")}
            session_columns = {column["name"] for column in inspector.get_columns("sessions")}
            user_indexes = {index["name"] for index in inspector.get_indexes("users")}
            session_indexes = {index["name"] for index in inspector.get_indexes("sessions")}
            audit_indexes = {index["name"] for index in inspector.get_indexes("audit_events")}
            with engine.connect() as connection:
                user = connection.execute(
                    text(
                        "SELECT email, is_blacklisted, created_at, updated_at, version "
                        "FROM users WHERE email = :email"
                    ),
                    {"email": legacy_email},
                ).mappings().one()
                session = connection.execute(
                    text(
                        "SELECT status, revoked_at, revoked_reason FROM sessions WHERE sid = :sid"
                    ),
                    {"sid": legacy_sid},
                ).mappings().one()

            _assert("audit_events" in tables, "audit_events table was not created")
            _assert(
                {"display_name", "is_blacklisted", "created_at", "updated_at", "version"} <= user_columns,
                "user management columns are missing after upgrade",
            )
            _assert(
                {"revoked_at", "revoked_reason"} <= session_columns,
                "session revocation columns are missing after upgrade",
            )
            _assert(user["is_blacklisted"] is False, "legacy user blacklist state was not backfilled")
            _assert(user["created_at"] is not None, "legacy user created_at was not backfilled")
            _assert(user["updated_at"] is not None, "legacy user updated_at was not backfilled")
            _assert(user["version"] == 1, "legacy user version was not backfilled")
            _assert(session["status"] == "active", "legacy session status changed during migration")
            _assert(session["revoked_at"] is None, "legacy session revoked_at must remain null")
            _assert(session["revoked_reason"] is None, "legacy session revoked_reason must remain null")
            _assert(
                "ix_users_is_active_is_blacklisted" in user_indexes,
                "user status index is missing",
            )
            _assert("ix_sessions_user_id_status" in session_indexes, "session status index is missing")
            _assert("ix_audit_events_target" in audit_indexes, "audit target index is missing")
        finally:
            engine.dispose()

        _alembic(database.url, "upgrade", "head")
        check_output = _alembic(database.url, "check")
        _assert(
            "No new upgrade operations detected" in check_output,
            "Alembic metadata check did not report a clean schema",
        )

        _alembic(database.url, "downgrade", "0001_initial_auth_schema")
        engine = create_engine(database.url, poolclass=NullPool)
        try:
            inspector = inspect(engine)
            _assert("audit_events" not in inspector.get_table_names(), "audit_events survived downgrade")
            user_columns = {column["name"] for column in inspector.get_columns("users")}
            session_columns = {column["name"] for column in inspector.get_columns("sessions")}
            _assert("is_blacklisted" not in user_columns, "user management columns survived downgrade")
            _assert("revoked_at" not in session_columns, "session revocation columns survived downgrade")
            with engine.connect() as connection:
                _assert(
                    connection.execute(
                        text("SELECT count(*) FROM users WHERE email = :email"),
                        {"email": legacy_email},
                    ).scalar_one()
                    == 1,
                    "legacy user was lost during downgrade",
                )
                _assert(
                    connection.execute(
                        text("SELECT count(*) FROM sessions WHERE sid = :sid"),
                        {"sid": legacy_sid},
                    ).scalar_one()
                    == 1,
                    "legacy session was lost during downgrade",
                )
        finally:
            engine.dispose()

        _alembic(database.url, "upgrade", "head")
        current_output = _alembic(database.url, "current")
        _assert("0005_permission_management" in current_output, "database did not return to the head revision")
        return {
            "database": database.name,
            "legacy_user_preserved": True,
            "legacy_session_preserved": True,
            "alembic_check": "clean",
            "current_revision": "0005_permission_management",
        }


def _find_available_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _wait_for_api(base_url: str, process: subprocess.Popen[str], timeout_seconds: float = 30) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise Phase4ValidationError("phase 4 API process exited before becoming healthy")
        try:
            response = httpx.get(f"{base_url}/health", timeout=2)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise Phase4ValidationError("timed out waiting for the phase 4 API process")


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


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


def _safe_response_body(response: httpx.Response) -> Any:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(body, dict):
        return {
            key: "[REDACTED]" if key in {"access_token", "refresh_token", "password", "new_password"} else value
            for key, value in body.items()
        }
    return body


def _expect(response: httpx.Response, expected_status: int, context: str) -> dict[str, Any]:
    if response.status_code != expected_status:
        raise Phase4ValidationError(
            f"{context} returned {response.status_code}, expected {expected_status}: "
            f"{_safe_response_body(response)}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise Phase4ValidationError(f"{context} did not return JSON") from exc
    _assert(isinstance(body, dict), f"{context} returned an unexpected response shape")
    return body


def _token_sid(access_token: str) -> str:
    payload = jwt.decode(access_token, options={"verify_signature": False})
    sid = payload.get("sid")
    if not isinstance(sid, str) or not sid:
        raise Phase4ValidationError("issued access token is missing sid")
    return sid


def _authorization(access_token: str, request_id: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {access_token}"}
    if request_id:
        headers["X-Request-ID"] = request_id
    return headers


def _assert_revoked_session(
    engine: Any,
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


def _login(client: httpx.Client, email: str, password: str, *, expected_status: int = 200) -> dict[str, Any]:
    response = client.post("/auth/login", json={"username": email, "password": password})
    if expected_status != 200:
        return _expect(response, expected_status, f"login for {email}")
    body = _expect(response, 200, f"login for {email}")
    _assert(isinstance(body.get("access_token"), str), "login response is missing access_token")
    _assert(isinstance(body.get("refresh_token"), str), "login response is missing refresh_token")
    return body


def _assert_access_and_refresh_revoked(
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
        client.post("/auth/refresh", json={"refresh_token": refresh_token}),
        401,
        "revoked refresh token check",
    )


def _run_http_management_flow(
    client: httpx.Client,
    engine: Any,
    redis_client: Redis,
    env: dict[str, str],
    suffix: str,
) -> dict[str, Any]:
    admin_login = _login(client, "admin@example.com", "password123")
    admin_access = str(admin_login["access_token"])
    admin_headers = _authorization(admin_access)

    _expect(client.get("/admin/users"), 401, "unauthenticated admin list")
    admin_me = _expect(client.get("/auth/me", headers=admin_headers), 200, "admin /auth/me")
    admin_id = int(admin_me["id"])

    initial_email = f"phase4-{suffix}@example.com"
    updated_email = f"phase4-updated-{suffix}@example.com"
    initial_password = "phase4-initial-password"
    replacement_password = "phase4-replacement-password"
    safe_responses: list[dict[str, Any]] = []

    created = _expect(
        client.post(
            "/admin/users",
            headers=_authorization(admin_access, "phase4-create"),
            json={
                "email": initial_email,
                "display_name": "Phase Four User",
                "password": initial_password,
            },
        ),
        201,
        "create user",
    )
    safe_responses.append(created)
    target_id = int(created["id"])
    _assert(created["version"] == 1, "created user did not start at version 1")

    listed = _expect(
        client.get("/admin/users", headers=admin_headers, params={"keyword": initial_email}),
        200,
        "list users",
    )
    safe_responses.append(listed)
    _assert(listed["total"] == 1, "created user was not returned by list filtering")
    detail = _expect(
        client.get(f"/admin/users/{target_id}", headers=admin_headers),
        200,
        "get user details",
    )
    safe_responses.append(detail)

    target_login = _login(client, initial_email, initial_password)
    _expect(
        client.get("/admin/users", headers=_authorization(str(target_login["access_token"]))),
        403,
        "permission boundary",
    )
    sid_email = _token_sid(str(target_login["access_token"]))

    updated = _expect(
        client.patch(
            f"/admin/users/{target_id}",
            headers=_authorization(admin_access, "phase4-update"),
            json={"email": updated_email, "display_name": "Updated Phase Four User", "version": 1},
        ),
        200,
        "update user",
    )
    safe_responses.append(updated)
    _assert(updated["changed"] is True, "email update did not report a change")
    _assert(updated["version"] == 2, "email update did not increment version")
    _assert(updated["revoked_sessions"] == 1, "email update did not revoke the active session")
    _assert_revoked_session(
        engine,
        redis_client,
        env["SESSION_PREFIX"],
        sid_email,
        "email_changed",
        86_400,
    )
    _assert_access_and_refresh_revoked(
        client,
        str(target_login["access_token"]),
        str(target_login["refresh_token"]),
    )

    target_login = _login(client, updated_email, initial_password)
    sid_disabled = _token_sid(str(target_login["access_token"]))
    disabled = _expect(
        client.post(
            f"/admin/users/{target_id}/disable",
            headers=_authorization(admin_access, "phase4-disable"),
            json={"reason": "phase 4 disable validation"},
        ),
        200,
        "disable user",
    )
    safe_responses.append(disabled)
    _assert(disabled["changed"] is True and disabled["is_active"] is False, "disable state is incorrect")
    _assert(disabled["revoked_sessions"] == 1, "disable did not revoke the active session")
    disabled_version = int(disabled["version"])
    _assert_revoked_session(
        engine,
        redis_client,
        env["SESSION_PREFIX"],
        sid_disabled,
        "user_disabled",
        86_400,
    )
    _assert_access_and_refresh_revoked(
        client,
        str(target_login["access_token"]),
        str(target_login["refresh_token"]),
    )
    _login(client, updated_email, initial_password, expected_status=401)

    disabled_again = _expect(
        client.post(
            f"/admin/users/{target_id}/disable",
            headers=_authorization(admin_access, "phase4-disable-retry"),
            json={"reason": "must not replace the first reason"},
        ),
        200,
        "repeat disable user",
    )
    safe_responses.append(disabled_again)
    _assert(disabled_again["changed"] is False, "repeat disable was not idempotent")
    _assert(disabled_again["revoked_sessions"] == 0, "repeat disable revoked sessions")
    _assert(disabled_again["version"] == disabled_version, "repeat disable changed the user version")
    _assert(
        disabled_again["disabled_reason"] == "phase 4 disable validation",
        "repeat disable replaced the original reason",
    )

    enabled = _expect(
        client.post(
            f"/admin/users/{target_id}/enable",
            headers=_authorization(admin_access, "phase4-enable"),
        ),
        200,
        "enable user",
    )
    safe_responses.append(enabled)
    _assert(enabled["is_active"] is True and enabled["is_blacklisted"] is False, "enable state is incorrect")
    _assert_revoked_session(
        engine,
        redis_client,
        env["SESSION_PREFIX"],
        sid_disabled,
        "user_disabled",
        86_400,
    )

    target_login = _login(client, updated_email, initial_password)
    sid_blacklisted = _token_sid(str(target_login["access_token"]))
    blacklisted = _expect(
        client.post(
            f"/admin/users/{target_id}/blacklist",
            headers=_authorization(admin_access, "phase4-blacklist"),
            json={"reason": "phase 4 blacklist validation"},
        ),
        200,
        "blacklist user",
    )
    safe_responses.append(blacklisted)
    _assert(blacklisted["is_blacklisted"] is True, "blacklist state was not set")
    _assert(blacklisted["is_active"] is True, "blacklist unexpectedly changed active state")
    _assert(blacklisted["revoked_sessions"] == 1, "blacklist did not revoke the active session")
    _assert_revoked_session(
        engine,
        redis_client,
        env["SESSION_PREFIX"],
        sid_blacklisted,
        "user_blacklisted",
        86_400,
    )
    _assert_access_and_refresh_revoked(
        client,
        str(target_login["access_token"]),
        str(target_login["refresh_token"]),
    )
    _login(client, updated_email, initial_password, expected_status=401)
    conflict = _expect(
        client.post(
            f"/admin/users/{target_id}/enable",
            headers=_authorization(admin_access, "phase4-enable-blacklisted"),
        ),
        409,
        "enable blacklisted user",
    )
    _assert(conflict.get("detail") == "USER_BLACKLISTED", "blacklisted enable returned the wrong error")

    recovered = _expect(
        client.post(
            f"/admin/users/{target_id}/recover",
            headers=_authorization(admin_access, "phase4-recover"),
        ),
        200,
        "recover user",
    )
    safe_responses.append(recovered)
    _assert(recovered["is_blacklisted"] is False, "recover did not clear blacklist state")
    _assert(recovered["is_active"] is True, "recover unexpectedly changed active state")
    _assert_revoked_session(
        engine,
        redis_client,
        env["SESSION_PREFIX"],
        sid_blacklisted,
        "user_blacklisted",
        86_400,
    )

    target_login = _login(client, updated_email, initial_password)
    sid_password = _token_sid(str(target_login["access_token"]))
    reset = _expect(
        client.post(
            f"/admin/users/{target_id}/reset-password",
            headers=_authorization(admin_access, "phase4-reset-password"),
            json={"new_password": replacement_password},
        ),
        200,
        "reset password",
    )
    safe_responses.append(reset)
    _assert(reset["revoked_sessions"] == 1, "password reset did not revoke the active session")
    _assert_revoked_session(
        engine,
        redis_client,
        env["SESSION_PREFIX"],
        sid_password,
        "password_reset",
        86_400,
    )
    _assert_access_and_refresh_revoked(
        client,
        str(target_login["access_token"]),
        str(target_login["refresh_token"]),
    )
    _login(client, updated_email, initial_password, expected_status=401)

    target_login = _login(client, updated_email, replacement_password)
    sid_logout = _token_sid(str(target_login["access_token"]))
    logged_out = _expect(
        client.post(
            f"/admin/users/{target_id}/force-logout",
            headers=_authorization(admin_access, "phase4-force-logout"),
            json={"reason": "phase 4 force logout validation"},
        ),
        200,
        "force logout",
    )
    safe_responses.append(logged_out)
    _assert(logged_out["revoked_sessions"] == 1, "force logout did not revoke the active session")
    _assert_revoked_session(
        engine,
        redis_client,
        env["SESSION_PREFIX"],
        sid_logout,
        "admin_force_logout",
        86_400,
    )
    _assert_access_and_refresh_revoked(
        client,
        str(target_login["access_token"]),
        str(target_login["refresh_token"]),
    )
    logged_out_again = _expect(
        client.post(
            f"/admin/users/{target_id}/force-logout",
            headers=_authorization(admin_access, "phase4-force-logout-retry"),
            json={"reason": "phase 4 retry"},
        ),
        200,
        "repeat force logout",
    )
    safe_responses.append(logged_out_again)
    _assert(logged_out_again["revoked_sessions"] == 0, "repeat force logout was not idempotent")

    with engine.connect() as connection:
        audits = connection.execute(
            text(
                "SELECT actor_user_id, action, target_id, result, reason, changes_json, request_id "
                "FROM audit_events WHERE target_id = :target_id ORDER BY id"
            ),
            {"target_id": target_id},
        ).mappings().all()
        sessions = connection.execute(
            text("SELECT count(*) FROM sessions WHERE user_id = :target_id AND status = 'revoked'"),
            {"target_id": target_id},
        ).scalar_one()

    expected_actions = {
        "user.created",
        "user.updated",
        "user.disabled",
        "user.enabled",
        "user.blacklisted",
        "user.recovered",
        "user.password_reset",
        "user.force_logout",
    }
    _assert(expected_actions <= {audit["action"] for audit in audits}, "management audit actions are incomplete")
    _assert(all(audit["actor_user_id"] == admin_id for audit in audits), "audit actor is incorrect")
    _assert(all(audit["target_id"] == target_id for audit in audits), "audit target is incorrect")
    expected_request_ids = {
        "phase4-create",
        "phase4-update",
        "phase4-disable",
        "phase4-enable",
        "phase4-blacklist",
        "phase4-recover",
        "phase4-reset-password",
        "phase4-force-logout",
    }
    _assert(expected_request_ids <= {audit["request_id"] for audit in audits}, "audit request IDs are incomplete")

    sensitive_values = {
        initial_password,
        replacement_password,
        str(admin_login["access_token"]),
        str(admin_login["refresh_token"]),
    }
    serialized_audits = json.dumps([dict(audit) for audit in audits], default=str)
    serialized_responses = json.dumps(safe_responses, default=str)
    _assert("hashed_password" not in serialized_responses, "admin response exposed hashed_password")
    _assert("hashed_password" not in serialized_audits, "audit changes exposed hashed_password")
    for secret in sensitive_values:
        _assert(secret not in serialized_audits, "audit record exposed a password or token")
        _assert(secret not in serialized_responses, "admin response exposed a password or token")

    return {
        "target_user_id": target_id,
        "audit_actions": sorted(expected_actions),
        "revoked_database_sessions": int(sessions),
        "redis_revocations_verified": 5,
        "permission_boundary": "401/403",
        "request_ids_verified": len(expected_request_ids),
    }


def run_management_flow(config: Phase4Config) -> dict[str, Any]:
    with temporary_postgres_database(config, "api") as database:
        suffix = database.name.rsplit("_", 1)[-1]
        redis_prefix = f"auth:phase4:{suffix}:"
        redis_client: Redis = Redis.from_url(config.redis_url, decode_responses=True)
        redis_client.ping()
        _clean_redis_namespace(redis_client, redis_prefix)
        env = _runtime_env(database.url, config.redis_url, redis_prefix)
        _alembic(database.url, "upgrade", "head")
        _seed(env)
        _seed(env)

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
        flow_error: Exception | None = None
        engine = create_engine(database.url, poolclass=NullPool)
        try:
            _wait_for_api(base_url, process)
            with httpx.Client(base_url=base_url, timeout=15) as client:
                result = _run_http_management_flow(client, engine, redis_client, env, suffix)
        except Exception as exc:
            flow_error = exc
        finally:
            engine.dispose()
            _stop_process(process)
            api_log.seek(0)
            log_text = api_log.read()
            api_log.close()
            _clean_redis_namespace(redis_client, redis_prefix)
            redis_client.close()

        sensitive_log_values = [
            "phase4-initial-password",
            "phase4-replacement-password",
            env["JWT_PRIVATE_KEY"],
        ]
        if flow_error is None:
            for secret in sensitive_log_values:
                _assert(secret not in log_text, "API logs exposed a password or private key")
        else:
            safe_error = _redact(str(flow_error), [*sensitive_log_values, *_database_secrets(database.url)])
            safe_log_tail = _redact("\n".join(log_text.splitlines()[-30:]), sensitive_log_values)
            raise Phase4ValidationError(f"management API validation failed: {safe_error}\nAPI log tail:\n{safe_log_tail}") from flow_error

        _assert(result is not None, "management API validation produced no result")
        return {
            "database": database.name,
            "redis_prefix": redis_prefix,
            **result,
            "temporary_resources_cleaned": True,
        }


def run_all_validations(config: Phase4Config) -> dict[str, dict[str, Any]]:
    return {
        "migration": run_migration_roundtrip(config),
        "management": run_management_flow(config),
    }


def _print_report(name: str, report: dict[str, Any]) -> None:
    safe_report = {key: value for key, value in report.items() if key not in {"redis_prefix"}}
    print(f"[PASS] {name}: {json.dumps(safe_report, sort_keys=True)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate user management phase 4 against isolated PostgreSQL and Redis")
    parser.add_argument(
        "--only",
        choices=("all", "migration", "management"),
        default="all",
        help="run all validations or one validation group",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = Phase4Config.from_env()
        if args.only in {"all", "migration"}:
            _print_report("Alembic migration roundtrip", run_migration_roundtrip(config))
        if args.only in {"all", "management"}:
            _print_report("PostgreSQL/Redis management API flow", run_management_flow(config))
    except (OSError, ValueError, Phase4ValidationError) as exc:
        safe_error = _redact(str(exc), _database_secrets(os.getenv("PHASE4_ADMIN_DATABASE_URL", "")))
        print(f"[FAIL] phase 4 validation: {safe_error}", file=sys.stderr)
        return 1
    print("Phase 4 validation completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
