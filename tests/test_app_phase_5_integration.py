from __future__ import annotations

import os

import pytest

from scripts.validate_app_phase_5 import (
    AppPhase5Config,
    run_concurrency_validation,
    run_http_validation,
    run_migration_validation,
)


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_APP_PHASE_5_INTEGRATION") != "1",
    reason="set RUN_APP_PHASE_5_INTEGRATION=1 to run isolated App PostgreSQL/Redis validation",
)


@pytest.fixture(scope="module")
def app_phase5_config() -> AppPhase5Config:
    return AppPhase5Config.from_env()


def test_app_phase5_migration_roundtrip(app_phase5_config: AppPhase5Config) -> None:
    report = run_migration_validation(app_phase5_config)

    assert report["current_revision"] == "0006_email_registration"
    assert report["alembic_check"] == "clean"
    assert report["legacy_user_preserved"] is True
    assert report["app_columns_verified"] == 14
    assert report["app_indexes_verified"] == 4
    assert report["temporary_resources_cleaned"] is True


def test_app_phase5_postgres_concurrency(app_phase5_config: AppPhase5Config) -> None:
    report = run_concurrency_validation(app_phase5_config)

    assert report["row_lock_waits_verified"] == 2
    assert report["disable_changes"] == [True, False]
    assert report["secret_rotations_serialized"] == 2
    assert report["optimistic_conflict"] == "APP_VERSION_CONFLICT"
    assert report["temporary_resources_cleaned"] is True


def test_app_phase5_real_jwt_lifecycle(app_phase5_config: AppPhase5Config) -> None:
    report = run_http_validation(app_phase5_config)

    assert report["permissions_verified"] == 6
    assert report["permission_denials_verified"] >= 8
    assert report["lifecycle_audits_verified"] == 5
    assert report["request_ids_verified"] == 5
    assert report["old_secret_invalidated"] is True
    assert report["one_time_secret_responses"] == 2
    assert report["real_jwt_permissions"] is True
    assert report["sensitive_log_scan"] == "clean"
    assert report["temporary_resources_cleaned"] is True
