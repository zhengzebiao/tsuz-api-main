from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import init_local


PRIVATE_KEY = "-----BEGIN PRIVATE KEY-----\nprivate\n-----END PRIVATE KEY-----\n"
PUBLIC_KEY = "-----BEGIN PUBLIC KEY-----\npublic\n-----END PUBLIC KEY-----\n"
TEMPLATE = "DATABASE_URL=postgres\nREDIS_URL=redis\nJWT_PRIVATE_KEY=\"__JWT_PRIVATE_KEY__\"\nJWT_PUBLIC_KEY=\"__JWT_PUBLIC_KEY__\"\n"


def write_template(path: Path) -> None:
    path.write_text(TEMPLATE)


def test_ensure_env_file_generates_keys_and_restricts_permissions(tmp_path: Path) -> None:
    template = tmp_path / ".env.local.example"
    env_file = tmp_path / ".env"
    write_template(template)

    created = init_local.ensure_env_file(env_file, template, lambda: (PRIVATE_KEY, PUBLIC_KEY))

    assert created is True
    content = env_file.read_text()
    assert "__JWT_" not in content
    assert "JWT_PRIVATE_KEY=\"-----BEGIN PRIVATE KEY-----\\nprivate\\n-----END PRIVATE KEY-----\"" in content
    assert "JWT_PUBLIC_KEY=\"-----BEGIN PUBLIC KEY-----\\npublic\\n-----END PUBLIC KEY-----\"" in content
    assert os.stat(env_file).st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("..env.*"))


def test_ensure_env_file_is_idempotent(tmp_path: Path) -> None:
    template = tmp_path / ".env.local.example"
    env_file = tmp_path / ".env"
    write_template(template)
    init_local.ensure_env_file(env_file, template, lambda: (PRIVATE_KEY, PUBLIC_KEY))
    original = env_file.read_text()

    key_generator = Mock(side_effect=AssertionError("keys must not be regenerated"))
    assert init_local.ensure_env_file(env_file, template, key_generator) is False
    assert env_file.read_text() == original
    key_generator.assert_not_called()


def test_validate_env_file_rejects_missing_values_and_placeholders(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('DATABASE_URL=postgres\nREDIS_URL=redis\nJWT_PRIVATE_KEY="CHANGE_ME"\n')
    os.chmod(env_file, 0o600)

    with pytest.raises(RuntimeError, match="missing required value"):
        init_local.validate_env_file(env_file)

    env_file.write_text(
        'DATABASE_URL=postgres\nREDIS_URL=redis\nJWT_PRIVATE_KEY="CHANGE_ME"\nJWT_PUBLIC_KEY="public"\n'
    )
    with pytest.raises(RuntimeError, match="placeholder"):
        init_local.validate_env_file(env_file)


def test_validate_env_file_rejects_insecure_permissions(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        'DATABASE_URL=postgres\nREDIS_URL=redis\nJWT_PRIVATE_KEY="private"\nJWT_PUBLIC_KEY="public"\n'
    )
    os.chmod(env_file, 0o644)

    with pytest.raises(RuntimeError, match="insecure permissions"):
        init_local.validate_env_file(env_file)


def test_ensure_api_image_builds_when_image_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[tuple[str, ...]] = []

    def fake_run(command: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "image-id\n", "")

    monkeypatch.setattr(init_local, "run_command", fake_run)

    init_local.ensure_api_image()

    assert commands == [
        ("docker", "compose", "images", "-q", "api"),
        ("docker", "compose", "build", "--pull=false", "api"),
    ]


def test_ensure_api_image_falls_back_only_when_an_image_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    existing_image = True

    def fake_run(command: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
        if "images" in command:
            return subprocess.CompletedProcess(command, 0, "image-id\n" if existing_image else "", "")
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(init_local, "run_command", fake_run)
    init_local.ensure_api_image()

    existing_image = False
    with pytest.raises(subprocess.CalledProcessError):
        init_local.ensure_api_image()


def test_wait_for_compose_services_requires_healthy_state(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    output = "\n".join(
        json.dumps({"Service": service, "State": "running", "Health": "healthy"})
        for service in ("postgres", "redis")
    )

    def fake_run(command: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(init_local, "run_command", fake_run)
    monkeypatch.setattr(init_local.time, "sleep", lambda _: None)

    init_local.wait_for_compose_services(("postgres", "redis"), timeout_seconds=1)

    assert calls == [("docker", "compose", "ps", "--all", "--format", "json", "postgres", "redis")]


def test_wait_for_compose_services_fails_on_unhealthy_state(monkeypatch: pytest.MonkeyPatch) -> None:
    output = json.dumps({"Service": "postgres", "State": "running", "Health": "unhealthy"})
    monkeypatch.setattr(
        init_local,
        "run_command",
        lambda command, **_: subprocess.CompletedProcess(command, 0, output, ""),
    )

    with pytest.raises(RuntimeError, match="postgres"):
        init_local.wait_for_compose_services(("postgres",), timeout_seconds=1)


def test_initialize_local_runs_commands_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(init_local, "check_prerequisites", lambda: None)
    monkeypatch.setattr(init_local, "ensure_env_file", lambda: False)
    monkeypatch.setattr(init_local, "ensure_api_image", lambda: commands.append(("ensure-image",)))
    monkeypatch.setattr(init_local, "wait_for_compose_services", lambda services, **_: commands.append(("wait", *services)))
    monkeypatch.setattr(init_local, "wait_for_health", lambda url, **_: commands.append(("health", url)))

    def fake_run(command: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(init_local, "run_command", fake_run)
    init_local.initialize_local(timeout_seconds=1)

    assert commands == [
        ("docker", "compose", "config", "--quiet"),
        ("docker", "compose", "up", "-d", "postgres", "redis"),
        ("wait", "postgres", "redis"),
        ("ensure-image",),
        ("docker", "compose", "run", "--rm", "--no-deps", "api", "python", "-m", "alembic", "upgrade", "head"),
        ("docker", "compose", "run", "--rm", "--no-deps", "api", "python", "-m", "app.seed"),
        (
            "docker",
            "compose",
            "run",
            "--rm",
            "--no-deps",
            "api",
            "python",
            "-m",
            "app.commands.sync_permissions",
        ),
        ("docker", "compose", "up", "-d", "--no-build", "api", "nginx"),
        ("wait", "api", "nginx"),
        ("health", init_local.API_HEALTH_URL),
        ("health", init_local.NGINX_HEALTH_URL),
    ]


def test_main_returns_nonzero_and_prints_diagnostics(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(init_local, "initialize_local", Mock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(init_local, "print_diagnostics", Mock())

    assert init_local.main([]) == 1
    assert "Error: boom" in capsys.readouterr().err
    init_local.print_diagnostics.assert_called_once()
