from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import string
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from redis import Redis
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.services.tencent_ses_service import EmailProviderError, TencentSesService

DEFAULT_ADMIN_DATABASE_URL = "postgresql+psycopg://test_user:test_password@127.0.0.1:5432/postgres"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
REDIS_IMAGE = "redis:7-alpine"


class Phase4EmailValidationError(RuntimeError):
    """Raised when isolated email authentication validation cannot complete."""


@dataclass(frozen=True)
class EmailPhase4Config:
    admin_database_url: str
    allow_remote: bool
    redis_image: str

    @classmethod
    def from_env(cls) -> EmailPhase4Config:
        config = cls(
            admin_database_url=os.getenv("PHASE4_EMAIL_ADMIN_DATABASE_URL", DEFAULT_ADMIN_DATABASE_URL),
            allow_remote=os.getenv("PHASE4_EMAIL_ALLOW_REMOTE", "0") == "1",
            redis_image=os.getenv("PHASE4_EMAIL_REDIS_IMAGE", REDIS_IMAGE),
        )
        config.validate()
        return config

    def validate(self) -> None:
        database_url = make_url(self.admin_database_url)
        if not database_url.drivername.startswith("postgresql"):
            raise Phase4EmailValidationError("PHASE4_EMAIL_ADMIN_DATABASE_URL must use PostgreSQL")
        if not self.allow_remote and database_url.host not in LOCAL_HOSTS:
            raise Phase4EmailValidationError(
                "phase 4 email validation only allows local PostgreSQL by default"
            )
        if not self.redis_image or any(char.isspace() for char in self.redis_image):
            raise Phase4EmailValidationError("PHASE4_EMAIL_REDIS_IMAGE is invalid")


@dataclass(frozen=True)
class TemporaryDatabase:
    name: str
    url: str


@dataclass(frozen=True)
class TemporaryRedis:
    container_name: str
    url: str


@dataclass(frozen=True)
class SesSmokeResult:
    preflight: dict[str, object]
    send: dict[str, object]


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise Phase4EmailValidationError(message)


def _redact(value: str, secrets_to_redact: Sequence[str] = ()) -> str:
    redacted = value
    for secret in secrets_to_redact:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _database_secrets(database_url: str) -> list[str]:
    if not database_url:
        return []
    url = make_url(database_url)
    return [database_url, url.password or ""]


def _run_command(
    command: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    secrets_to_redact: Sequence[str] = (),
) -> str:
    completed = subprocess.run(
        list(command),
        cwd=ROOT_DIR,
        env=env or os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )
    output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    if completed.returncode != 0:
        safe_output = _redact(output, secrets_to_redact)
        raise Phase4EmailValidationError(
            f"command failed ({' '.join(command)}): {safe_output[-6000:]}"
        )
    return output


def _quoted_identifier(identifier: str) -> str:
    if not identifier.replace("_", "").isalnum():
        raise Phase4EmailValidationError("generated database name is invalid")
    return f'"{identifier}"'


def _database_url_for(admin_database_url: str, database_name: str) -> str:
    return make_url(admin_database_url).set(database=database_name).render_as_string(hide_password=False)


@contextmanager
def temporary_postgres_database(config: EmailPhase4Config) -> Iterator[TemporaryDatabase]:
    database_name = f"tsuz_email_phase4_{uuid4().hex[:12]}"
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
        yield TemporaryDatabase(database_name, _database_url_for(config.admin_database_url, database_name))
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


def _redis_host_port(container_name: str) -> int:
    output = _run_command(("docker", "port", container_name, "6379/tcp"))
    mapping = output.strip().splitlines()[0]
    host_port = mapping.rsplit(":", 1)[-1]
    try:
        return int(host_port)
    except ValueError as exc:
        raise Phase4EmailValidationError("temporary Redis port could not be resolved") from exc


@contextmanager
def temporary_redis(config: EmailPhase4Config) -> Iterator[TemporaryRedis]:
    container_name = f"tsuz-email-redis-{uuid4().hex[:12]}"
    try:
        _run_command(
            (
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                container_name,
                "--publish",
                "127.0.0.1::6379",
                config.redis_image,
            )
        )
        port = _redis_host_port(container_name)
        url = f"redis://127.0.0.1:{port}/0"
        client = Redis.from_url(url, decode_responses=True)
        deadline = time.monotonic() + 20
        while True:
            try:
                client.ping()
                break
            except Exception as exc:
                if time.monotonic() >= deadline:
                    raise Phase4EmailValidationError("temporary Redis did not become ready") from exc
                time.sleep(0.25)
        client.close()
        yield TemporaryRedis(container_name, url)
    finally:
        subprocess.run(
            ("docker", "rm", "--force", container_name),
            cwd=ROOT_DIR,
            text=True,
            capture_output=True,
            check=False,
        )


def _run_alembic(database_url: str, *arguments: str) -> str:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    return _run_command(
        (sys.executable, "-m", "alembic", *arguments),
        env=env,
        secrets_to_redact=_database_secrets(database_url),
    )


def _integration_env(database_url: str, redis_url: str, suffix: str) -> dict[str, str]:
    env = os.environ.copy()
    prefix = f"auth:phase4-email:{suffix}:"
    env.update(
        {
            "RUN_PHASE4_EMAIL_INTEGRATION": "1",
            "DATABASE_URL": database_url,
            "REDIS_URL": redis_url,
            "REDIS_KEY_PREFIX": prefix,
            "EMAIL_CHALLENGE_PREFIX": f"{prefix}email:challenge:",
            "EMAIL_SEND_LIMIT_PREFIX": f"{prefix}email:send:",
            "EMAIL_IP_SEND_LIMIT_PREFIX": f"{prefix}email:ip-send:",
            "TOKEN_BLACKLIST_PREFIX": f"{prefix}blacklist:jti:",
            "REFRESH_TOKEN_PREFIX": f"{prefix}refresh:",
            "SESSION_PREFIX": f"{prefix}session:",
            "REFRESH_TOKEN_EXPIRE_DAYS": "1",
            "REFRESH_TOKEN_REUSE_GRACE_SECONDS": "0",
            "WEB_CONCURRENCY": "1",
        }
    )
    return env


def run_isolated_email_integration(config: EmailPhase4Config) -> dict[str, object]:
    with temporary_postgres_database(config) as database, temporary_redis(config) as redis:
        suffix = database.name.rsplit("_", 1)[-1]
        env = _integration_env(database.url, redis.url, suffix)
        _run_alembic(database.url, "upgrade", "head")
        _run_alembic(database.url, "check")
        output = _run_command(
            (sys.executable, "-m", "pytest", "tests/test_phase_4_email_integration.py", "-q"),
            env=env,
            secrets_to_redact=_database_secrets(database.url),
        )
        return {
            "temporary_database": database.name,
            "temporary_redis": True,
            "pytest": output.splitlines()[-1] if output.splitlines() else "passed",
            "resources_cleaned": True,
        }


def _mask_email(email: str) -> str:
    local, separator, domain = email.strip().partition("@")
    if not separator:
        return "***"
    return f"{local[:1] or '*'}***@{domain[:1]}***"


def _safe_request_id(value: object) -> str:
    text_value = str(value or "")
    return text_value[:12] if text_value else "unknown"


def _ses_request(model_name: str):
    from tencentcloud.ses.v20201002 import models

    return getattr(models, model_name)()


def _ses_call(client: object, method: str, request: object) -> object:
    try:
        return getattr(client, method)(request)
    except Exception as exc:
        # Tencent SDK exception text can contain request parameters or addresses.
        raise Phase4EmailValidationError(f"SES preflight failed at {method}") from exc


def _run_ses_preflight(service: TencentSesService) -> dict[str, object]:
    domain = settings.email_from_address.rsplit("@", 1)[-1].lower()
    client = service.client

    identity_request = _ses_request("GetEmailIdentityRequest")
    identity_request.EmailIdentity = domain
    identity = _ses_call(client, "GetEmailIdentity", identity_request)
    _assert(
        bool(getattr(identity, "VerifiedForSendingStatus", False)),
        "SES sending identity is not verified",
    )
    dns_attributes = list(getattr(identity, "Attributes", None) or [])
    dns_status = {
        str(getattr(attribute, "Type", "unknown")): bool(getattr(attribute, "Status", False))
        for attribute in dns_attributes
    }
    _assert(bool(dns_attributes) and all(dns_status.values()), "SES DNS verification is incomplete")

    sender_response = _ses_call(client, "ListEmailAddress", _ses_request("ListEmailAddressRequest"))
    sender_addresses = {
        str(getattr(sender, "EmailAddress", "")).lower()
        for sender in (getattr(sender_response, "EmailSenders", None) or [])
    }
    _assert(settings.email_from_address.lower() in sender_addresses, "SES sender address is not available")

    template_request = _ses_request("GetEmailTemplateRequest")
    template_request.TemplateID = settings.email_template_id
    template = _ses_call(client, "GetEmailTemplate", template_request)
    _assert(getattr(template, "TemplateStatus", None) == 0, "SES template is not approved")
    content = getattr(template, "TemplateContent", None)
    content_parts: list[str] = []
    for value in (getattr(content, "Text", None), getattr(content, "Html", None)):
        if not value:
            continue
        decoded = str(value)
        try:
            decoded = base64.b64decode(decoded, validate=True).decode("utf-8", errors="replace")
        except (ValueError, UnicodeDecodeError):
            pass
        content_parts.append(decoded)
    content_text = " ".join(content_parts).lower()
    _assert("code" in content_text, "SES template is missing code variable")
    _assert("expire_minutes" in content_text, "SES template is missing expire_minutes variable")

    identities_request = _ses_request("ListEmailIdentitiesRequest")
    identities_request.Limit = 100
    identities_request.Offset = 0
    identities_response = _ses_call(client, "ListEmailIdentities", identities_request)
    matching_identity = next(
        (
            item
            for item in (getattr(identities_response, "EmailIdentities", None) or [])
            if str(getattr(item, "IdentityName", "")).lower() == domain
        ),
        None,
    )
    daily_quota = getattr(matching_identity, "DailyQuota", None)
    if daily_quota is None:
        daily_quota = getattr(identities_response, "MaxDailyQuota", None)
    _assert(isinstance(daily_quota, int) and daily_quota > 0, "SES daily quota is unavailable")

    return {
        "domain_verified": True,
        "dns_status": dns_status,
        "sender_available": True,
        "template_approved": True,
        "template_variables": ["code", "expire_minutes"],
        "daily_quota_available": True,
        "identity_request_id": _safe_request_id(getattr(identity, "RequestId", None)),
        "template_request_id": _safe_request_id(getattr(template, "RequestId", None)),
    }


def run_real_ses_smoke() -> SesSmokeResult:
    if os.getenv("RUN_PHASE4_REAL_SES") != "1":
        return SesSmokeResult(
            preflight={"status": "skipped", "reason": "RUN_PHASE4_REAL_SES is not 1"},
            send={"status": "skipped"},
        )

    recipient = os.getenv("PHASE4_SES_RECIPIENT", "").strip()
    if not recipient:
        raise Phase4EmailValidationError(
            "PHASE4_SES_RECIPIENT is required when RUN_PHASE4_REAL_SES=1"
        )
    _assert(settings.tencentcloud_region == "ap-guangzhou", "SES region must be ap-guangzhou")
    _assert(
        settings.tencentcloud_ses_endpoint == "ses.tencentcloudapi.com",
        "SES endpoint must be ses.tencentcloudapi.com",
    )
    _assert(settings.email_template_id == 57044, "SES template ID must be 57044")
    _assert(settings.email_from_address == "noreply@notify.tusz.online", "SES sender address is unexpected")
    _assert(settings.email_from_name == "tusz.online", "SES sender name is unexpected")

    try:
        service = TencentSesService()
        preflight = _run_ses_preflight(service)
        code = "".join(secrets.choice(string.digits) for _ in range(settings.email_code_length))
        result = service.send_verification_email(recipient, code, purpose="phase4_smoke")
    except EmailProviderError as exc:
        raise Phase4EmailValidationError("SES send failed") from exc

    status_summary: dict[str, object] = {"status": "sent"}
    if result.message_id:
        status_request = _ses_request("GetSendEmailStatusRequest")
        status_request.RequestDate = datetime.now(UTC).date().isoformat()
        status_request.Offset = 0
        status_request.MessageId = result.message_id
        status_request.Limit = 1
        try:
            status_response = _ses_call(service.client, "GetSendEmailStatus", status_request)
        except Phase4EmailValidationError:
            status_summary["status_check"] = "unavailable"
        else:
            statuses = getattr(status_response, "EmailStatusList", None) or []
            if statuses:
                status = statuses[0]
                status_summary["send_status"] = str(getattr(status, "SendStatus", "unknown"))
                status_summary["deliver_status"] = str(getattr(status, "DeliverStatus", "unknown"))
            status_summary["status_request_id"] = _safe_request_id(getattr(status_response, "RequestId", None))

    return SesSmokeResult(
        preflight=preflight,
        send={
            **status_summary,
            "message_id": _safe_request_id(result.message_id),
            "request_id": _safe_request_id(result.request_id),
            "recipient": _mask_email(recipient),
        },
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate email authentication on temporary PostgreSQL and Redis infrastructure"
    )
    parser.add_argument(
        "--only",
        choices=("all", "integration", "ses"),
        default="all",
        help="run isolated integration, SES smoke, or both",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = EmailPhase4Config.from_env()
        if args.only in {"all", "integration"}:
            print(
                "[PASS] isolated email integration: "
                + json.dumps(run_isolated_email_integration(config), sort_keys=True)
            )
        if args.only in {"all", "ses"}:
            smoke = run_real_ses_smoke()
            print("[PASS] SES smoke: " + json.dumps({"preflight": smoke.preflight, "send": smoke.send}, sort_keys=True))
    except (OSError, ValueError, Phase4EmailValidationError) as exc:
        safe_error = _redact(str(exc), _database_secrets(os.getenv("PHASE4_EMAIL_ADMIN_DATABASE_URL", "")))
        print(f"[FAIL] phase 4 email validation: {safe_error}", file=sys.stderr)
        return 1
    print("Phase 4 email validation completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
