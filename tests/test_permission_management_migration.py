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
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
HEAD_REVISION = "0007_qq_login"

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_PERMISSION_MANAGEMENT_PHASE_1_MIGRATION") != "1",
    reason="set RUN_PERMISSION_MANAGEMENT_PHASE_1_MIGRATION=1 to run isolated PostgreSQL migration validation",
)


class PermissionMigrationValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MigrationConfig:
    admin_database_url: str
    allow_remote: bool
    allow_default_port: bool

    @classmethod
    def from_env(cls) -> MigrationConfig:
        database_url = os.getenv(
            "PERMISSION_PHASE1_ADMIN_DATABASE_URL",
            "postgresql+psycopg://test_user:test_password@127.0.0.1:55432/postgres",
        )
        config = cls(
            admin_database_url=database_url,
            allow_remote=os.getenv("PERMISSION_PHASE1_ALLOW_REMOTE", "0") == "1",
            allow_default_port=os.getenv("PERMISSION_PHASE1_ALLOW_DEFAULT_PORT", "0") == "1",
        )
        config.validate()
        return config

    def validate(self) -> None:
        database_url = make_url(self.admin_database_url)
        if not database_url.drivername.startswith("postgresql"):
            raise PermissionMigrationValidationError(
                "PERMISSION_PHASE1_ADMIN_DATABASE_URL must use PostgreSQL"
            )
        if not self.allow_remote and database_url.host not in LOCAL_HOSTS:
            raise PermissionMigrationValidationError(
                "permission migration validation only allows local PostgreSQL by default"
            )
        if not self.allow_default_port and (database_url.port or 5432) == 5432:
            raise PermissionMigrationValidationError(
                "permission migration validation refuses PostgreSQL 5432 by default"
            )


@dataclass(frozen=True)
class TemporaryDatabase:
    name: str
    url: str


def _quoted_identifier(identifier: str) -> str:
    if not identifier.replace("_", "").isalnum():
        raise PermissionMigrationValidationError("generated database name is invalid")
    return f'"{identifier}"'


def _database_url_for(admin_database_url: str, database_name: str) -> str:
    return make_url(admin_database_url).set(database=database_name).render_as_string(hide_password=False)


@contextmanager
def temporary_postgres_database(config: MigrationConfig):
    database_name = f"tsuz_permission_phase1_migration_{uuid4().hex[:12]}"
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
        raise PermissionMigrationValidationError(
            f"alembic {' '.join(arguments)} failed:\n{safe_output}"
        )
    return output


def _permission_columns(engine) -> dict[str, dict]:
    return {column["name"]: column for column in inspect(engine).get_columns("permissions")}


def test_permission_management_migration_roundtrip() -> None:
    config = MigrationConfig.from_env()
    with temporary_postgres_database(config) as database:
        _run_alembic(database.url, "upgrade", "0004_role_management")
        engine = create_engine(database.url, poolclass=NullPool)
        legacy_permission_name = f"legacy:permission_{uuid4().hex[:8]}"
        legacy_role_name = f"legacy_permission_role_{uuid4().hex[:8]}"
        try:
            with engine.begin() as connection:
                role_id = connection.execute(
                    text("INSERT INTO roles (name) VALUES (:name) RETURNING id"),
                    {"name": legacy_role_name},
                ).scalar_one()
                permission_id = connection.execute(
                    text(
                        "INSERT INTO permissions (name, description) "
                        "VALUES (:name, 'Legacy permission description') RETURNING id"
                    ),
                    {"name": legacy_permission_name},
                ).scalar_one()
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
            inspector = inspect(engine)
            columns = _permission_columns(engine)
            indexes = {index["name"]: index for index in inspector.get_indexes("permissions")}
            endpoint_columns = {
                column["name"]: column
                for column in inspector.get_columns("permission_endpoints")
            }
            endpoint_indexes = {
                index["name"]: index
                for index in inspector.get_indexes("permission_endpoints")
            }
            endpoint_primary_key = inspector.get_pk_constraint("permission_endpoints")
            endpoint_foreign_keys = inspector.get_foreign_keys("permission_endpoints")

            assert set(columns) == {
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
            for column_name in (
                "id",
                "name",
                "display_name",
                "description",
                "is_declared",
                "is_enabled",
                "created_at",
                "updated_at",
                "version",
            ):
                assert columns[column_name]["nullable"] is False
            for column_name in ("disabled_at", "disabled_reason", "missing_at"):
                assert columns[column_name]["nullable"] is True
            assert {"ix_permissions_id", "ix_permissions_name", "ix_permissions_is_declared", "ix_permissions_is_enabled"} <= set(indexes)
            assert indexes["ix_permissions_name"]["unique"] is True
            assert indexes["ix_permissions_is_declared"]["unique"] is False
            assert indexes["ix_permissions_is_enabled"]["unique"] is False

            assert set(endpoint_columns) == {"permission_id", "http_method", "path", "route_name"}
            assert all(column["nullable"] is False for column in endpoint_columns.values())
            assert endpoint_primary_key["constrained_columns"] == [
                "permission_id",
                "http_method",
                "path",
            ]
            assert len(endpoint_foreign_keys) == 1
            assert endpoint_foreign_keys[0]["referred_table"] == "permissions"
            assert endpoint_foreign_keys[0]["referred_columns"] == ["id"]
            assert endpoint_foreign_keys[0]["options"]["ondelete"] == "CASCADE"
            assert endpoint_indexes["ix_permission_endpoints_http_method_path"]["unique"] is False
            assert endpoint_indexes["ix_permission_endpoints_http_method_path"]["column_names"] == [
                "http_method",
                "path",
            ]

            with engine.begin() as connection:
                legacy = connection.execute(
                    text(
                        "SELECT id, name, display_name, description, is_declared, is_enabled, "
                        "disabled_at, disabled_reason, missing_at, created_at, updated_at, version "
                        "FROM permissions WHERE id = :permission_id"
                    ),
                    {"permission_id": permission_id},
                ).mappings().one()
                association_count = connection.execute(
                    text(
                        "SELECT count(*) FROM role_permissions "
                        "WHERE role_id = :role_id AND permission_id = :permission_id"
                    ),
                    {"role_id": role_id, "permission_id": permission_id},
                ).scalar_one()
                next_permission = connection.execute(
                    text(
                        "INSERT INTO permissions (name, description) "
                        "VALUES (:name, '') RETURNING id, display_name, is_declared, "
                        "is_enabled, created_at, updated_at, version"
                    ),
                    {"name": f"new:permission_{uuid4().hex[:8]}"},
                ).mappings().one()
                connection.execute(
                    text(
                        "INSERT INTO permission_endpoints "
                        "(permission_id, http_method, path, route_name) "
                        "VALUES (:permission_id, 'GET', '/admin/permissions', 'list_permissions')"
                    ),
                    {"permission_id": permission_id},
                )

            assert legacy["id"] == permission_id
            assert legacy["name"] == legacy_permission_name
            assert legacy["display_name"] == legacy_permission_name
            assert legacy["description"] == "Legacy permission description"
            assert legacy["is_declared"] is True
            assert legacy["is_enabled"] is True
            assert legacy["disabled_at"] is None
            assert legacy["disabled_reason"] is None
            assert legacy["missing_at"] is None
            assert legacy["created_at"] is not None
            assert legacy["updated_at"] is not None
            assert legacy["version"] == 1
            assert association_count == 1
            assert next_permission["id"] == permission_id + 1
            assert next_permission["display_name"] == ""
            assert next_permission["is_declared"] is True
            assert next_permission["is_enabled"] is True
            assert next_permission["created_at"] is not None
            assert next_permission["updated_at"] is not None
            assert next_permission["version"] == 1

            with pytest.raises(IntegrityError), engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO permission_endpoints "
                        "(permission_id, http_method, path, route_name) "
                        "VALUES (:permission_id, 'GET', '/admin/permissions', 'duplicate')"
                    ),
                    {"permission_id": permission_id},
                )
        finally:
            engine.dispose()

        check_output = _run_alembic(database.url, "check")
        assert "No new upgrade operations detected" in check_output

        _run_alembic(database.url, "downgrade", "0004_role_management")
        engine = create_engine(database.url, poolclass=NullPool)
        try:
            inspector = inspect(engine)
            assert set(_permission_columns(engine)) == {"id", "name", "description"}
            assert not inspector.has_table("permission_endpoints")
            assert "ix_permissions_is_declared" not in {
                index["name"] for index in inspector.get_indexes("permissions")
            }
            assert "ix_permissions_is_enabled" not in {
                index["name"] for index in inspector.get_indexes("permissions")
            }
            with engine.connect() as connection:
                preserved = connection.execute(
                    text(
                        "SELECT p.id, p.name, p.description, count(rp.role_id) AS associations "
                        "FROM permissions p "
                        "LEFT JOIN role_permissions rp ON rp.permission_id = p.id "
                        "WHERE p.id = :permission_id "
                        "GROUP BY p.id, p.name, p.description"
                    ),
                    {"permission_id": permission_id},
                ).mappings().one()
            assert preserved["id"] == permission_id
            assert preserved["name"] == legacy_permission_name
            assert preserved["description"] == "Legacy permission description"
            assert preserved["associations"] == 1
        finally:
            engine.dispose()

        _run_alembic(database.url, "upgrade", HEAD_REVISION)
        final_check_output = _run_alembic(database.url, "check")
        current_output = _run_alembic(database.url, "current")
        assert "No new upgrade operations detected" in final_check_output
        assert HEAD_REVISION in current_output

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
                association_count = connection.execute(
                    text(
                        "SELECT count(*) FROM role_permissions "
                        "WHERE role_id = :role_id AND permission_id = :permission_id"
                    ),
                    {"role_id": role_id, "permission_id": permission_id},
                ).scalar_one()
            assert restored["id"] == permission_id
            assert restored["display_name"] == legacy_permission_name
            assert restored["is_declared"] is True
            assert restored["is_enabled"] is True
            assert restored["version"] == 1
            assert association_count == 1
        finally:
            engine.dispose()
