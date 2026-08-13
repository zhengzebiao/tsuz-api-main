from __future__ import annotations

import os

import pytest

from scripts.validate_role_management_phase_5 import (
    RolePhase5Config,
    run_concurrency_validation,
    run_http_validation,
    run_migration_validation,
)


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_ROLE_MANAGEMENT_PHASE_5_INTEGRATION") != "1",
    reason=(
        "set RUN_ROLE_MANAGEMENT_PHASE_5_INTEGRATION=1 to run isolated "
        "role PostgreSQL/Redis validation"
    ),
)


@pytest.fixture(scope="module")
def role_phase5_config() -> RolePhase5Config:
    return RolePhase5Config.from_env()


def test_role_phase5_migration_roundtrip(role_phase5_config: RolePhase5Config) -> None:
    report = run_migration_validation(role_phase5_config)

    assert report["current_revision"] == "0004_role_management"
    assert report["alembic_check"] == "clean"
    assert report["role_columns_verified"] == 9
    assert report["role_indexes_verified"] == 3
    assert report["legacy_role_data_preserved"] is True
    assert report["role_associations_preserved"] is True
    assert report["temporary_resources_cleaned"] is True


def test_role_phase5_postgres_concurrency(role_phase5_config: RolePhase5Config) -> None:
    report = run_concurrency_validation(role_phase5_config)

    assert report["row_lock_waits_verified"] == 3
    assert report["disable_changes"] == [True, False]
    assert report["enable_changes"] == [True, False]
    assert report["role_assignment_conflict"] == "USER_VERSION_CONFLICT"
    assert report["role_update_conflict"] == "ROLE_VERSION_CONFLICT"
    assert report["associations_consistent"] is True
    assert report["temporary_resources_cleaned"] is True


def test_role_phase5_real_jwt_redis_and_http(role_phase5_config: RolePhase5Config) -> None:
    report = run_http_validation(role_phase5_config)

    assert report["permissions_verified"] == 6
    assert report["permission_denials_verified"] >= 8
    assert report["lifecycle_audits_verified"] == 5
    assert report["request_ids_verified"] == 5
    assert report["redis_revocations_verified"] == 2
    assert report["old_sessions_rejected"] is True
    assert report["disabled_role_claim_filter"] is True
    assert report["reenabled_role_claim_restore"] is True
    assert report["user_role_replacement_verified"] is True
    assert report["seed_idempotency_verified"] is True
    assert report["real_jwt_permissions"] is True
    assert report["sensitive_log_scan"] == "clean"
    assert report["temporary_resources_cleaned"] is True
