from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.main import create_app
from app.models.permission import Permission
from app.models.permission_endpoint import PermissionEndpoint
from app.models.role import Role, role_permissions
from app.seed.__main__ import seed
from app.services.permission_scanner import scan_permission_routes
from app.services.permission_sync_service import PermissionSyncService

ROOT_DIR = Path(__file__).resolve().parents[1]
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_PERMISSION_SYNC_CONCURRENCY") != "1",
    reason=(
        "set RUN_PERMISSION_SYNC_CONCURRENCY=1 to run isolated PostgreSQL "
        "permission synchronization concurrency validation"
    ),
)


class PermissionSyncConcurrencyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConcurrencyConfig:
    admin_database_url: str
    allow_remote: bool
    allow_default_port: bool

    @classmethod
    def from_env(cls) -> ConcurrencyConfig:
        config = cls(
            admin_database_url=os.getenv(
                "PERMISSION_SYNC_ADMIN_DATABASE_URL",
                "postgresql+psycopg://test_user:test_password@127.0.0.1:55432/postgres",
            ),
            allow_remote=os.getenv("PERMISSION_SYNC_ALLOW_REMOTE", "0") == "1",
            allow_default_port=(
                os.getenv("PERMISSION_SYNC_ALLOW_DEFAULT_PORT", "0") == "1"
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        url = make_url(self.admin_database_url)
        if not url.drivername.startswith("postgresql"):
            raise PermissionSyncConcurrencyError(
                "PERMISSION_SYNC_ADMIN_DATABASE_URL must use PostgreSQL"
            )
        if not self.allow_remote and url.host not in LOCAL_HOSTS:
            raise PermissionSyncConcurrencyError(
                "permission sync validation only allows local PostgreSQL by default"
            )
        if not self.allow_default_port and (url.port or 5432) == 5432:
            raise PermissionSyncConcurrencyError(
                "permission sync validation refuses PostgreSQL 5432 by default"
            )


@dataclass(frozen=True)
class TemporaryDatabase:
    name: str
    url: str


def _quoted_identifier(identifier: str) -> str:
    if not identifier.replace("_", "").isalnum():
        raise PermissionSyncConcurrencyError("generated database name is invalid")
    return f'"{identifier}"'


@contextmanager
def temporary_postgres_database(config: ConcurrencyConfig):
    database_name = f"tsuz_permission_sync_{uuid4().hex[:12]}"
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
        url = make_url(config.admin_database_url).set(database=database_name)
        yield TemporaryDatabase(
            name=database_name,
            url=url.render_as_string(hide_password=False),
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


def _run_alembic(database_url: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    completed = subprocess.run(
        (sys.executable, "-m", "alembic", "upgrade", "head"),
        cwd=ROOT_DIR,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        password = make_url(database_url).password or ""
        output = f"{completed.stdout}\n{completed.stderr}".replace(
            database_url,
            "[REDACTED_DATABASE_URL]",
        )
        if password:
            output = output.replace(password, "[REDACTED]")
        raise PermissionSyncConcurrencyError(f"alembic upgrade failed:\n{output}")


def test_postgresql_advisory_lock_serializes_concurrent_sync() -> None:
    config = ConcurrencyConfig.from_env()
    catalog = scan_permission_routes(create_app())
    with temporary_postgres_database(config) as database:
        _run_alembic(database.url)
        engine = create_engine(database.url, poolclass=NullPool)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        try:
            with SessionLocal() as db:
                seed(db)
                db.commit()

            first_locked = threading.Event()
            release_first = threading.Event()
            second_started = threading.Event()
            lock_waits: dict[int, float] = {}

            def synchronize(index: int) -> int:
                with SessionLocal() as db:
                    service = PermissionSyncService(db)
                    outer_plan = service.build_plan(catalog)
                    original_lock = service._acquire_advisory_lock

                    def observed_lock() -> None:
                        lock_started = time.monotonic()
                        original_lock()
                        lock_waits[index] = time.monotonic() - lock_started
                        if index == 1:
                            first_locked.set()
                            if not release_first.wait(timeout=10):
                                raise PermissionSyncConcurrencyError(
                                    "timed out holding the first advisory lock"
                                )

                    service._acquire_advisory_lock = observed_lock  # type: ignore[method-assign]
                    if index == 2:
                        second_started.set()
                    summary = service.apply_plan(outer_plan)
                    db.commit()
                    return summary.created

            with ThreadPoolExecutor(max_workers=2) as executor:
                first_future = executor.submit(synchronize, 1)
                assert first_locked.wait(timeout=10)
                second_future = executor.submit(synchronize, 2)
                assert second_started.wait(timeout=10)
                time.sleep(0.3)
                assert not second_future.done()
                release_first.set()
                first_created = first_future.result(timeout=15)
                second_created = second_future.result(timeout=15)

            assert first_created == 21
            assert second_created == 0
            assert lock_waits[2] >= 0.25

            with SessionLocal() as db:
                assert db.scalar(select(func.count()).select_from(Permission)) == 21
                assert db.scalar(
                    select(func.count()).select_from(PermissionEndpoint)
                ) == 26
                admin_role = db.scalar(select(Role).where(Role.name == "admin"))
                assert admin_role is not None
                assert db.scalar(
                    select(func.count())
                    .select_from(role_permissions)
                    .where(role_permissions.c.role_id == admin_role.id)
                ) == 21
                final_plan = PermissionSyncService(db).build_plan(catalog)
                assert final_plan.has_changes is False
        finally:
            engine.dispose()
