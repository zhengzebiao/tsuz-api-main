from __future__ import annotations

import os

import pytest

from scripts.validate_phase_4 import Phase4Config
from scripts.validate_qq_phase_4 import run_qq_integration

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_PHASE4_QQ_INTEGRATION") != "1",
    reason="set RUN_PHASE4_QQ_INTEGRATION=1 to run isolated PostgreSQL/Redis QQ validation",
)


def test_qq_oauth_route_flow_on_isolated_postgres_and_redis() -> None:
    report = run_qq_integration(Phase4Config.from_env())

    assert report == {
        "authorization": True,
        "callback": True,
        "fixed_consumer": True,
        "ticket_exchange": True,
        "bearer_me": True,
        "replay_and_expiry": True,
        "disabled_and_blacklisted": True,
        "provider_failure": True,
        "temporary_resources_cleaned": True,
    }
