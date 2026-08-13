from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

import app.commands.sync_permissions as command
from app.services.permission_scanner import PermissionScanError, PermissionScanResult
from app.services.permission_sync_service import PermissionSyncPlan, PermissionSyncSummary


class FakeDb:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closes += 1


@dataclass
class ServiceState:
    plan: PermissionSyncPlan
    summary: PermissionSyncSummary
    build_calls: int = 0
    apply_calls: int = 0


def make_plan(*, has_changes: bool) -> PermissionSyncPlan:
    scan_result = PermissionScanResult(permission_names=(), bindings=(), routes=())
    return PermissionSyncPlan(
        scan_result=scan_result,
        created=("app:read",) if has_changes else (),
        restored=(),
        marked_missing=(),
        endpoint_bindings_added=(),
        endpoint_bindings_removed=(),
        admin_grants_added=(),
        unchanged=() if has_changes else ("app:read",),
        permission_updates=("app:read",) if has_changes else (),
        session_user_ids=(),
    )


def setup_command(
    monkeypatch: pytest.MonkeyPatch,
    *,
    plan: PermissionSyncPlan,
    summary: PermissionSyncSummary | None = None,
) -> tuple[FakeDb, ServiceState]:
    db = FakeDb()
    state = ServiceState(
        plan=plan,
        summary=summary
        or PermissionSyncSummary(
            created=len(plan.created),
            restored=0,
            marked_missing=0,
            endpoint_bindings_added=0,
            endpoint_bindings_removed=0,
            admin_grants_added=0,
            sessions_revoked=0,
            unchanged=len(plan.unchanged),
        ),
    )

    class FakeService:
        def __init__(self, received_db: FakeDb) -> None:
            assert received_db is db

        def build_plan(self, scan_result: PermissionScanResult) -> PermissionSyncPlan:
            assert scan_result == plan.scan_result
            state.build_calls += 1
            return state.plan

        def apply_plan(self, received_plan: PermissionSyncPlan) -> PermissionSyncSummary:
            assert received_plan is state.plan
            state.apply_calls += 1
            return state.summary

    monkeypatch.setattr(command, "configure_logging", lambda: None)
    monkeypatch.setattr(command, "create_app", lambda: object())
    monkeypatch.setattr(command, "scan_permission_routes", lambda application: plan.scan_result)
    monkeypatch.setattr(command, "SessionLocal", lambda: db)
    monkeypatch.setattr(command, "PermissionSyncService", FakeService)
    return db, state


def test_default_command_applies_commits_and_outputs_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db, state = setup_command(monkeypatch, plan=make_plan(has_changes=True))

    exit_code = command.run()

    assert exit_code == 0
    assert state.build_calls == 1
    assert state.apply_calls == 1
    assert db.commits == 1
    assert db.rollbacks == 0
    assert db.closes == 1
    assert json.loads(capsys.readouterr().out) == state.summary.to_dict()


def test_dry_run_only_builds_and_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db, state = setup_command(monkeypatch, plan=make_plan(has_changes=True))

    exit_code = command.run(dry_run=True)

    assert exit_code == 0
    assert state.build_calls == 1
    assert state.apply_calls == 0
    assert db.commits == 0
    assert db.rollbacks == 0
    assert db.closes == 1
    output = json.loads(capsys.readouterr().out)
    assert output["created"] == ["app:read"]
    assert output["has_changes"] is True


@pytest.mark.parametrize(
    ("has_changes", "expected_exit_code"),
    [(False, 0), (True, command.CHECK_DIFFERENCES_EXIT_CODE)],
)
def test_check_exit_code_is_based_on_read_only_plan(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    has_changes: bool,
    expected_exit_code: int,
) -> None:
    db, state = setup_command(monkeypatch, plan=make_plan(has_changes=has_changes))

    assert command.run(check=True) == expected_exit_code

    assert state.apply_calls == 0
    assert db.commits == 0
    assert db.rollbacks == 0
    assert json.loads(capsys.readouterr().out)["has_changes"] is has_changes


def test_application_error_rolls_back_and_redacts_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db, _state = setup_command(monkeypatch, plan=make_plan(has_changes=True))

    class FailingService:
        def __init__(self, received_db: FakeDb) -> None:
            assert received_db is db

        def build_plan(self, scan_result: PermissionScanResult) -> PermissionSyncPlan:
            return make_plan(has_changes=True)

        def apply_plan(self, plan: PermissionSyncPlan) -> PermissionSyncSummary:
            raise RuntimeError(
                "postgresql+psycopg://user:secret@db.example/test access-token-value"
            )

    monkeypatch.setattr(command, "PermissionSyncService", FailingService)

    assert command.run() == command.COMMAND_ERROR_EXIT_CODE

    assert db.commits == 0
    assert db.rollbacks == 1
    assert db.closes == 1
    output = capsys.readouterr().out
    assert "RuntimeError" in output
    assert "secret" not in output
    assert "access-token-value" not in output
    assert "db.example" not in output


def test_scan_error_fails_before_database_session_is_created(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(command, "configure_logging", lambda: None)
    monkeypatch.setattr(command, "create_app", lambda: object())
    monkeypatch.setattr(
        command,
        "scan_permission_routes",
        lambda application: (_ for _ in ()).throw(PermissionScanError("bad route")),
    )
    monkeypatch.setattr(
        command,
        "SessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not open")),
    )

    assert command.run() == command.COMMAND_ERROR_EXIT_CODE
    output = capsys.readouterr().out
    assert "PermissionScanError" in output
    assert "bad route" not in output


def test_parser_rejects_dry_run_and_check_together() -> None:
    with pytest.raises(SystemExit):
        command.build_parser().parse_args(["--dry-run", "--check"])
