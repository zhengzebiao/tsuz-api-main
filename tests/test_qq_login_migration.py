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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

ROOT_DIR = Path(__file__).resolve().parents[1]
BASE_REVISION = "0006_email_registration"
HEAD_REVISION = "0007_qq_login"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_QQ_LOGIN_MIGRATION") != "1",
    reason="set RUN_QQ_LOGIN_MIGRATION=1 to run isolated PostgreSQL QQ migration validation",
)


class QQLoginMigrationValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MigrationConfig:
    admin_database_url: str
    allow_remote: bool
    allow_default_port: bool

    @classmethod
    def from_env(cls) -> MigrationConfig:
        database_url = os.getenv(
            "QQ_LOGIN_ADMIN_DATABASE_URL",
            "postgresql+psycopg://test_user:test_password@127.0.0.1:55432/postgres",
        )
        config = cls(
            admin_database_url=database_url,
            allow_remote=os.getenv("QQ_LOGIN_ALLOW_REMOTE", "0") == "1",
            allow_default_port=os.getenv("QQ_LOGIN_ALLOW_DEFAULT_PORT", "0") == "1",
        )
        config.validate()
        return config

    def validate(self) -> None:
        database_url = make_url(self.admin_database_url)
        if not database_url.drivername.startswith("postgresql"):
            raise QQLoginMigrationValidationError("QQ_LOGIN_ADMIN_DATABASE_URL must use PostgreSQL")
        if not self.allow_remote and database_url.host not in LOCAL_HOSTS:
            raise QQLoginMigrationValidationError(
                "QQ login migration validation only allows local PostgreSQL by default"
            )
        if not self.allow_default_port and (database_url.port or 5432) == 5432:
            raise QQLoginMigrationValidationError(
                "QQ login migration validation refuses PostgreSQL 5432 by default"
            )


@dataclass(frozen=True)
class TemporaryDatabase:
    name: str
    url: str


def _quoted_identifier(identifier: str) -> str:
    if not identifier.replace("_", "").isalnum():
        raise QQLoginMigrationValidationError("generated database name is invalid")
    return f'"{identifier}"'


def _database_url_for(admin_database_url: str, database_name: str) -> str:
    return make_url(admin_database_url).set(database=database_name).render_as_string(hide_password=False)


@contextmanager
def temporary_postgres_database(config: MigrationConfig):
    database_name = f"tsuz_qq_login_{uuid4().hex[:12]}"
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


def _run_alembic(database_url: str, *arguments: str, expected_success: bool = True) -> str:
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
    password = make_url(database_url).password or ""
    safe_output = output.replace(database_url, "[REDACTED_DATABASE_URL]")
    if password:
        safe_output = safe_output.replace(password, "[REDACTED]")
    if expected_success and completed.returncode != 0:
        raise QQLoginMigrationValidationError(
            f"alembic {' '.join(arguments)} failed:\n{safe_output}"
        )
    if not expected_success and completed.returncode == 0:
        raise QQLoginMigrationValidationError(
            f"alembic {' '.join(arguments)} unexpectedly succeeded"
        )
    return safe_output


def test_qq_login_migration_roundtrip() -> None:
    config = MigrationConfig.from_env()
    with temporary_postgres_database(config) as database:
        _run_alembic(database.url, "upgrade", BASE_REVISION)
        engine = create_engine(database.url, poolclass=NullPool)
        legacy_email = f"legacy-{uuid4().hex[:8]}@example.com"
        try:
            with engine.begin() as connection:
                legacy_user_id = connection.execute(
                    text(
                        "INSERT INTO users "
                        "(email, hashed_password, is_active, is_blacklisted, version) "
                        "VALUES (:email, 'legacy-hash', true, false, 1) RETURNING id"
                    ),
                    {"email": legacy_email},
                ).scalar_one()
        finally:
            engine.dispose()

        _run_alembic(database.url, "upgrade", HEAD_REVISION)
        engine = create_engine(database.url, poolclass=NullPool)
        try:
            inspector = inspect(engine)
            user_columns = {column["name"]: column for column in inspector.get_columns("users")}
            identity_columns = {
                column["name"]: column for column in inspector.get_columns("user_identities")
            }
            identity_indexes = {
                index["name"]: index for index in inspector.get_indexes("user_identities")
            }
            identity_unique_constraints = {
                constraint["name"]: constraint
                for constraint in inspector.get_unique_constraints("user_identities")
            }
            identity_foreign_keys = inspector.get_foreign_keys("user_identities")

            assert user_columns["email"]["nullable"] is True
            assert user_columns["hashed_password"]["nullable"] is True
            assert set(identity_columns) == {
                "id",
                "user_id",
                "provider",
                "provider_subject",
                "display_name",
                "avatar",
                "verified",
                "created_at",
                "updated_at",
                "last_login_at",
            }
            assert identity_unique_constraints[
                "uq_user_identities_provider_provider_subject"
            ]["column_names"] == ["provider", "provider_subject"]
            assert identity_indexes["ix_user_identities_user_id"]["column_names"] == ["user_id"]
            assert identity_indexes["ix_user_identities_user_id_provider"]["column_names"] == [
                "user_id",
                "provider",
            ]
            assert identity_foreign_keys[0]["referred_table"] == "users"
            assert identity_foreign_keys[0]["options"]["ondelete"] == "CASCADE"

            with engine.begin() as connection:
                legacy = connection.execute(
                    text("SELECT email, hashed_password FROM users WHERE id = :user_id"),
                    {"user_id": legacy_user_id},
                ).mappings().one()
                qq_user_id = connection.execute(
                    text(
                        "INSERT INTO users "
                        "(email, hashed_password, display_name, is_active, is_blacklisted, version) "
                        "VALUES (NULL, NULL, 'QQ user', true, false, 1) RETURNING id"
                    )
                ).scalar_one()
                connection.execute(
                    text(
                        "INSERT INTO user_identities "
                        "(user_id, provider, provider_subject, display_name, verified) "
                        "VALUES (:user_id, 'qq', 'openid-123', 'QQ user', true)"
                    ),
                    {"user_id": qq_user_id},
                )
            assert legacy["email"] == legacy_email
            assert legacy["hashed_password"] == "legacy-hash"

            with pytest.raises(IntegrityError), engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO user_identities "
                        "(user_id, provider, provider_subject, verified) "
                        "VALUES (:user_id, 'qq', 'openid-123', true)"
                    ),
                    {"user_id": legacy_user_id},
                )
        finally:
            engine.dispose()

        assert "No new upgrade operations detected" in _run_alembic(database.url, "check")

        downgrade_output = _run_alembic(
            database.url,
            "downgrade",
            BASE_REVISION,
            expected_success=False,
        )
        assert "cannot downgrade 0007_qq_login" in downgrade_output

        engine = create_engine(database.url, poolclass=NullPool)
        try:
            inspector = inspect(engine)
            assert inspector.has_table("user_identities")
            assert {column["name"]: column for column in inspector.get_columns("users")}["email"][
                "nullable"
            ] is True
            with engine.begin() as connection:
                assert connection.execute(
                    text("SELECT count(*) FROM user_identities WHERE provider = 'qq'")
                ).scalar_one() == 1
                connection.execute(text("DELETE FROM user_identities"))
                connection.execute(
                    text("DELETE FROM users WHERE email IS NULL OR hashed_password IS NULL")
                )
        finally:
            engine.dispose()

        _run_alembic(database.url, "downgrade", BASE_REVISION)
        engine = create_engine(database.url, poolclass=NullPool)
        try:
            inspector = inspect(engine)
            assert not inspector.has_table("user_identities")
            user_columns = {column["name"]: column for column in inspector.get_columns("users")}
            assert user_columns["email"]["nullable"] is False
            assert user_columns["hashed_password"]["nullable"] is False
            with engine.connect() as connection:
                assert connection.execute(
                    text("SELECT count(*) FROM users WHERE email = :email"),
                    {"email": legacy_email},
                ).scalar_one() == 1
        finally:
            engine.dispose()

        _run_alembic(database.url, "upgrade", "head")
        assert HEAD_REVISION in _run_alembic(database.url, "current")
        assert "No new upgrade operations detected" in _run_alembic(database.url, "check")
