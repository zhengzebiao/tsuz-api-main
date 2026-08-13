from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT_DIR / ".env"
ENV_TEMPLATE = ROOT_DIR / ".env.local.example"
API_HEALTH_URL = "http://127.0.0.1:8000/health"
NGINX_HEALTH_URL = "http://127.0.0.1:8080/health"
ENV_FILE_MODE = 0o600
REQUIRED_ENV_VALUES = (
    "DATABASE_URL",
    "REDIS_URL",
    "JWT_PRIVATE_KEY",
    "JWT_PUBLIC_KEY",
)
PLACEHOLDER_VALUES = frozenset(
    {
        "__JWT_PRIVATE_KEY__",
        "__JWT_PUBLIC_KEY__",
        "CHANGE_ME",
        "-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----",
        "-----BEGIN PUBLIC KEY-----\\n...\\n-----END PUBLIC KEY-----",
    }
)


def run_command(
    command: Sequence[str],
    *,
    cwd: Path = ROOT_DIR,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    print(f"+ {' '.join(command)}", flush=True)
    return subprocess.run(
        list(command),
        cwd=cwd,
        check=True,
        text=True,
        capture_output=capture_output,
    )


def check_prerequisites() -> None:
    missing = [command for command in ("docker", "openssl") if shutil.which(command) is None]
    if missing:
        raise RuntimeError(f"missing required command(s): {', '.join(missing)}")
    run_command(("docker", "compose", "version"), capture_output=True)


def escape_env_value(value: str) -> str:
    return value.strip().replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def generate_jwt_keys() -> tuple[str, str]:
    with tempfile.TemporaryDirectory(prefix="tsuz-api-main-init-") as temporary_directory:
        private_key_path = Path(temporary_directory) / "jwt-private.pem"
        public_key_path = Path(temporary_directory) / "jwt-public.pem"
        run_command(
            (
                "openssl",
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
                "-out",
                str(private_key_path),
            ),
            capture_output=True,
        )
        run_command(
            ("openssl", "pkey", "-in", str(private_key_path), "-pubout", "-out", str(public_key_path)),
            capture_output=True,
        )
        return private_key_path.read_text(), public_key_path.read_text()


def parse_env(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"')
    return values


def validate_env_file(path: Path = ENV_FILE) -> None:
    if not path.is_file():
        raise RuntimeError(f"environment file does not exist: {path}")
    if path.stat().st_mode & 0o077:
        raise RuntimeError(f"{path.name} has insecure permissions; run chmod 600 {path.name} before initializing")

    values = parse_env(path.read_text())
    missing = [key for key in REQUIRED_ENV_VALUES if not values.get(key)]
    if missing:
        raise RuntimeError(f"{path.name} is missing required value(s): {', '.join(missing)}")
    placeholders = [key for key in REQUIRED_ENV_VALUES if values[key] in PLACEHOLDER_VALUES]
    if placeholders:
        raise RuntimeError(f"{path.name} contains placeholder value(s): {', '.join(placeholders)}")


def ensure_env_file(
    env_file: Path = ENV_FILE,
    template_file: Path = ENV_TEMPLATE,
    key_generator: Callable[[], tuple[str, str]] = generate_jwt_keys,
) -> bool:
    if env_file.exists():
        validate_env_file(env_file)
        print(f"Keeping existing {env_file.name}; no values were overwritten.")
        return False
    if not template_file.is_file():
        raise RuntimeError(f"local environment template does not exist: {template_file}")

    private_key, public_key = key_generator()
    content = template_file.read_text()
    if "__JWT_PRIVATE_KEY__" not in content or "__JWT_PUBLIC_KEY__" not in content:
        raise RuntimeError(f"{template_file.name} is missing JWT key placeholders")
    content = content.replace("__JWT_PRIVATE_KEY__", escape_env_value(private_key))
    content = content.replace("__JWT_PUBLIC_KEY__", escape_env_value(public_key))

    env_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=env_file.parent,
            prefix=f".{env_file.name}.",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            os.chmod(temporary_path, ENV_FILE_MODE)
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        validate_env_file(temporary_path)
        try:
            os.link(temporary_path, env_file)
        except FileExistsError:
            validate_env_file(env_file)
            print(f"Keeping existing {env_file.name}; no values were overwritten.")
            return False
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    os.chmod(env_file, ENV_FILE_MODE)
    print(f"Created {env_file.name} with local JWT keys and restricted permissions.")
    return True


def ensure_api_image() -> None:
    image = run_command(("docker", "compose", "images", "-q", "api"), capture_output=True)
    try:
        run_command(("docker", "compose", "build", "--pull=false", "api"))
    except subprocess.CalledProcessError:
        if not image.stdout.strip():
            raise
        print("Warning: API image rebuild failed; using the existing local image.", file=sys.stderr)


def wait_for_compose_services(
    services: Sequence[str],
    *,
    timeout_seconds: float = 120,
    interval_seconds: float = 2,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    requested = set(services)
    while time.monotonic() < deadline:
        completed = run_command(
            ("docker", "compose", "ps", "--all", "--format", "json", *services),
            capture_output=True,
        )
        states: dict[str, dict[str, str]] = {}
        for line in completed.stdout.splitlines():
            try:
                service = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = service.get("Service")
            if name:
                states[name] = service

        failed = [
            service
            for service in services
            if service in states and states[service].get("State") in {"exited", "dead"}
        ]
        unhealthy = [service for service in services if service in states and states[service].get("Health") == "unhealthy"]
        if failed or unhealthy:
            details = ", ".join(failed + unhealthy)
            raise RuntimeError(f"Docker service(s) failed health checks: {details}")
        if requested.issubset(states) and all(
            states[service].get("State") == "running" and states[service].get("Health") == "healthy"
            for service in services
        ):
            return
        time.sleep(interval_seconds)
    raise RuntimeError(f"timed out waiting for Docker service(s): {', '.join(services)}")


def wait_for_health(
    url: str,
    *,
    timeout_seconds: float = 120,
    interval_seconds: float = 2,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=min(5, max(1, timeout_seconds))) as response:
                if response.status == 200:
                    print(f"Health check passed: {url}")
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(interval_seconds)
    raise RuntimeError(f"timed out waiting for API health check: {url}")


def print_diagnostics() -> None:
    print("Initialization failed. Docker status and recent logs:", file=sys.stderr)
    for command in (
        ("docker", "compose", "ps"),
        ("docker", "compose", "logs", "--tail", "80", "api", "postgres", "redis", "nginx"),
    ):
        try:
            run_command(command)
        except (OSError, subprocess.CalledProcessError):
            pass


def initialize_local(*, timeout_seconds: float = 120) -> None:
    if timeout_seconds <= 0:
        raise RuntimeError("timeout must be greater than zero")
    check_prerequisites()
    ensure_env_file()
    run_command(("docker", "compose", "config", "--quiet"))
    run_command(("docker", "compose", "up", "-d", "postgres", "redis"))
    wait_for_compose_services(("postgres", "redis"), timeout_seconds=timeout_seconds)
    ensure_api_image()
    run_command(("docker", "compose", "run", "--rm", "--no-deps", "api", "python", "-m", "alembic", "upgrade", "head"))
    run_command(("docker", "compose", "run", "--rm", "--no-deps", "api", "python", "-m", "app.seed"))
    run_command(
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
        )
    )
    run_command(("docker", "compose", "up", "-d", "--no-build", "api", "nginx"))
    wait_for_compose_services(("api", "nginx"), timeout_seconds=timeout_seconds)
    wait_for_health(API_HEALTH_URL, timeout_seconds=timeout_seconds)
    wait_for_health(NGINX_HEALTH_URL, timeout_seconds=timeout_seconds)
    print("Local initialization completed.")
    print("API: http://127.0.0.1:8000  Docs: http://127.0.0.1:8000/docs  nginx: http://127.0.0.1:8080")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Initialize the local Docker development environment.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=120,
        help="maximum seconds to wait for Docker services and HTTP health checks (default: 120)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        initialize_local(timeout_seconds=args.timeout)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        print_diagnostics()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
