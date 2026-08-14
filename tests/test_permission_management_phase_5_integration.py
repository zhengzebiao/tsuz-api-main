from __future__ import annotations

import os

import pytest

from scripts.validate_permission_management_phase_5 import (
    PermissionPhase5Config,
    run_concurrency_validation,
    run_http_validation,
    run_migration_validation,
)


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_PERMISSION_MANAGEMENT_PHASE_5_INTEGRATION") != "1",
    reason=(
        "set RUN_PERMISSION_MANAGEMENT_PHASE_5_INTEGRATION=1 to run isolated "
        "permission PostgreSQL/Redis validation"
    ),
)


@pytest.fixture(scope="module")
def permission_phase5_config() -> PermissionPhase5Config:
    return PermissionPhase5Config.from_env()


def test_permission_phase5_migration_roundtrip(
    permission_phase5_config: PermissionPhase5Config,
) -> None:
    report = run_migration_validation(permission_phase5_config)

    assert report["current_revision"] == "0006_email_registration"
    assert report["alembic_check"] == "clean"
    assert report["permission_columns_verified"] == 12
    assert report["permission_indexes_verified"] == 4
    assert report["endpoint_columns_verified"] == 4
    assert report["legacy_permission_data_preserved"] is True
    assert report["role_associations_preserved"] is True
    assert report["sequence_preserved"] is True
    assert report["duplicate_endpoint_rejected"] is True
    assert report["temporary_resources_cleaned"] is True


def test_permission_phase5_postgres_concurrency_and_redis_rollback(
    permission_phase5_config: PermissionPhase5Config,
) -> None:
    report = run_concurrency_validation(permission_phase5_config)

    assert report["advisory_lock_waits_verified"] == 1
    assert report["row_lock_waits_verified"] == 2
    assert report["sync_counts_verified"] == [26, 33, 26]
    assert report["disable_changes"] == [True, False]
    assert report["enable_changes"] == [True, False]
    assert report["distinct_session_revocations"] == 1
    assert report["permission_update_conflict"] == "PERMISSION_VERSION_CONFLICT"
    assert report["redis_failure_rollback_verified"] is True
    assert report["sync_retry_idempotency_verified"] is True
    assert report["associations_consistent"] is True
    assert report["temporary_resources_cleaned"] is True


def test_permission_phase5_real_jwt_redis_sync_and_http(
    permission_phase5_config: PermissionPhase5Config,
) -> None:
    report = run_http_validation(permission_phase5_config)

    assert report["permissions_verified"] == 4
    assert report["permission_denials_verified"] >= 6
    assert report["catalog_counts"] == [26, 33, 26]
    assert report["lifecycle_audits_verified"] == 3
    assert report["request_ids_verified"] == 3
    assert report["redis_revocations_verified"] == 5
    assert report["old_sessions_rejected"] is True
    assert report["disabled_scope_filter"] is True
    assert report["missing_scope_filter"] is True
    assert report["database_status_denial"] is True
    assert report["reenabled_scope_restore"] is True
    assert report["missing_restore_identity_preserved"] is True
    assert report["seed_sync_idempotency_verified"] is True
    assert report["real_rs256_permissions"] is True
    assert report["sensitive_log_scan"] == "clean"
    assert report["temporary_resources_cleaned"] is True
