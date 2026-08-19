from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

ROOT_DIR = Path(__file__).resolve().parents[1]
BASE_REVISION = "0005_permission_management"
HEAD_REVISION = "0007_qq_login"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_EMAIL_REGISTRATION_MIGRATION") != "1",
    reason="set RUN_EMAIL_REGISTRATION_MIGRATION=1 to run isolated PostgreSQL migration validation",
)


class EmailRegistrationMigrationValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MigrationConfig:
    admin_database_url: str
    allow_remote: bool
    allow_default_port: bool

    @classmethod
    def from_env(cls) -> MigrationConfig:
        database_url = os.getenv(
            "EMAIL_REGISTRATION_ADMIN_DATABASE_URL",
            "postgresql+psycopg://test_user:test_password@127.0.0.1:55432/postgres",
        )
        config = cls(
            admin_database_url=database_url,
            allow_remote=os.getenv("EMAIL_REGISTRATION_ALLOW_REMOTE", "0") == "1",
            allow_default_port=os.getenv("EMAIL_REGISTRATION_ALLOW_DEFAULT_PORT", "0") == "1",
        )
        config.validate()
        return config

    def validate(self) -> None:
        database_url = make_url(self.admin_database_url)
        if not database_url.drivername.startswith("postgresql"):
            raise EmailRegistrationMigrationValidationError(
                "EMAIL_REGISTRATION_ADMIN_DATABASE_URL must use PostgreSQL"
            )
        if not self.allow_remote and database_url.host not in LOCAL_HOSTS:
            raise EmailRegistrationMigrationValidationError(
                "email registration migration validation only allows local PostgreSQL by default"
            )
        if not self.allow_default_port and (database_url.port or 5432) == 5432:
            raise EmailRegistrationMigrationValidationError(
                "email registration migration validation refuses PostgreSQL 5432 by default"
            )


@dataclass(frozen=True)
class TemporaryDatabase:
    name: str
    url: str


def _quoted_identifier(identifier: str) -> str:
    if not identifier.replace("_", "").isalnum():
        raise EmailRegistrationMigrationValidationError("generated database name is invalid")
    return f'"{identifier}"'


def _database_url_for(admin_database_url: str, database_name: str) -> str:
    return make_url(admin_database_url).set(database=database_name).render_as_string(hide_password=False)


@contextmanager
def temporary_postgres_database(config: MigrationConfig):
    database_name = f"tsuz_email_registration_{uuid4().hex[:12]}"
    admin_engine = create_engine(
        config.admin_database_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    created = False
    try:
        with admin_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            connection.exec_driver_sql(f"CREATE DATABASE {_quoted_identifier(database_name)}")
            created = True
        yield TemporaryDatabase(
            name=database_name,
            url=_database_url_for(config.admin_database_url, database_name),
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


def _run_alembic(database_url: str, *arguments: str) -> str:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    completed = subprocess.run(
        (sys.executable, "-m", "alembic", *arguments),
        cwd=ROOT_DIR,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    if completed.returncode != 0:
        password = make_url(database_url).password or ""
        safe_output = output.replace(database_url, "[REDACTED_DATABASE_URL]")
        if password:
            safe_output = safe_output.replace(password, "[REDACTED]")
        raise EmailRegistrationMigrationValidationError(
            f"alembic {' '.join(arguments)} failed:\n{safe_output}"
        )
    return output


def test_email_registration_migration_roundtrip() -> None:
    config = MigrationConfig.from_env()
    with temporary_postgres_database(config) as database:
        _run_alembic(database.url, "upgrade", BASE_REVISION)
        engine = create_engine(database.url, poolclass=NullPool)
        legacy_email = f"legacy-{uuid4().hex[:8]}@example.com"
        normal_role_name = "normal"
        permission_name = f"user:read-{uuid4().hex[:8]}"
        try:
            with engine.begin() as connection:
                user_id = connection.execute(
                    text(
                        "INSERT INTO users (email, hashed_password, is_active, is_blacklisted, created_at, updated_at, version) "
                        "VALUES (:email, 'legacy-hash', true, false, '2026-08-14 10:00:00', '2026-08-14 10:00:00', 1) "
                        "RETURNING id"
                    ),
                    {"email": legacy_email},
                ).scalar_one()
                role_id = connection.execute(
                    text(
                        "INSERT INTO roles (name, is_enabled) VALUES (:name, true) RETURNING id"
                    ),
                    {"name": normal_role_name},
                ).scalar_one()
                permission_id = connection.execute(
                    text(
                        "INSERT INTO permissions (name, display_name, description, is_declared, is_enabled) "
                        "VALUES (:name, 'Read users', '', true, true) RETURNING id"
                    ),
                    {"name": permission_name},
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

        _run_alembic(database.url, "upgrade", HEAD_REVISION)
        engine = create_engine(database.url, poolclass=NullPool)
        try:
            columns = {column["name"]: column for column in inspect(engine).get_columns("users")}
            assert "email_verified_at" in columns
            assert columns["email_verified_at"]["nullable"] is True
            with engine.connect() as connection:
                legacy = connection.execute(
                    text(
                        "SELECT email, hashed_password, email_verified_at FROM users WHERE id = :user_id"
                    ),
                    {"user_id": user_id},
                ).mappings().one()
                role = connection.execute(
                    text("SELECT name, is_enabled FROM roles WHERE id = :role_id"),
                    {"role_id": role_id},
                ).mappings().one()
                valid_permission_count = connection.execute(
                    text(
                        "SELECT count(*) FROM role_permissions rp "
                        "JOIN roles r ON r.id = rp.role_id "
                        "JOIN permissions p ON p.id = rp.permission_id "
                        "WHERE r.id = :role_id AND r.is_enabled = true "
                        "AND p.is_enabled = true AND p.is_declared = true"
                    ),
                    {"role_id": role_id},
                ).scalar_one()
                nullable_user_id = connection.execute(
                    text(
                        "INSERT INTO users (email, hashed_password, is_active, is_blacklisted, version) "
                        "VALUES (:email, 'new-hash', true, false, 1) RETURNING id"
                    ),
                    {"email": f"new-{uuid4().hex[:8]}@example.com"},
                ).scalar_one()
                nullable_verified_at = connection.execute(
                    text("SELECT email_verified_at FROM users WHERE id = :user_id"),
                    {"user_id": nullable_user_id},
                ).scalar_one()
            assert legacy["email"] == legacy_email
            assert legacy["hashed_password"] == "legacy-hash"
            assert legacy["email_verified_at"] is not None
            assert role["name"] == normal_role_name
            assert role["is_enabled"] is True
            assert valid_permission_count == 1
            assert nullable_verified_at is None
        finally:
            engine.dispose()

        check_output = _run_alembic(database.url, "check")
        assert "No new upgrade operations detected" in check_output

        _run_alembic(database.url, "downgrade", BASE_REVISION)
        engine = create_engine(database.url, poolclass=NullPool)
        try:
            assert "email_verified_at" not in {
                column["name"] for column in inspect(engine).get_columns("users")
            }
            with engine.connect() as connection:
                assert connection.execute(
                    text("SELECT count(*) FROM users WHERE email = :email"),
                    {"email": legacy_email},
                ).scalar_one() == 1
                assert connection.execute(
                    text("SELECT count(*) FROM roles WHERE id = :role_id"),
                    {"role_id": role_id},
                ).scalar_one() == 1
                assert connection.execute(
                    text("SELECT count(*) FROM role_permissions WHERE role_id = :role_id"),
                    {"role_id": role_id},
                ).scalar_one() == 1
        finally:
            engine.dispose()

        _run_alembic(database.url, "upgrade", "head")
        current_output = _run_alembic(database.url, "current")
        assert HEAD_REVISION in current_output
        assert "No new upgrade operations detected" in _run_alembic(database.url, "check")
