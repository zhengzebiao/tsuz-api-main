from __future__ import annotations

import os

import pytest

from scripts.validate_phase_4 import Phase4Config, run_management_flow, run_migration_roundtrip


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_PHASE_4_INTEGRATION") != "1",
    reason="set RUN_PHASE_4_INTEGRATION=1 to run isolated PostgreSQL/Redis validation",
)


@pytest.fixture(scope="module")
def phase4_config() -> Phase4Config:
    return Phase4Config.from_env()


def test_phase4_alembic_roundtrip_preserves_legacy_data(phase4_config: Phase4Config) -> None:
    report = run_migration_roundtrip(phase4_config)

    assert report["legacy_user_preserved"] is True
    assert report["legacy_session_preserved"] is True
    assert report["alembic_check"] == "clean"
    assert report["current_revision"] == "0007_qq_login"


def test_phase4_management_flow_verifies_postgres_redis_and_http(phase4_config: Phase4Config) -> None:
    report = run_management_flow(phase4_config)

    assert report["temporary_resources_cleaned"] is True
    assert report["permission_boundary"] == "401/403"
    assert report["redis_revocations_verified"] >= 5
    assert report["request_ids_verified"] >= 8
    assert report["revoked_database_sessions"] >= 5
