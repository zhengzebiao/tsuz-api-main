from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from redis import Redis
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session as DbSession

import app.services.refresh_token_service as refresh_token_module
import app.services.session_service as session_module
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.security import sha256_text
from app.models.permission import Permission
from app.models.role import Role, role_permissions, user_roles
from app.models.session import Session as AuthSession
from app.models.user import User
from app.services.auth_service import AuthService
from app.services.email_auth_service import EmailAuthService
from app.services.verification_challenge_service import VerificationChallengeService


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_PHASE4_EMAIL_INTEGRATION") != "1",
    reason="set RUN_PHASE4_EMAIL_INTEGRATION=1 to run isolated PostgreSQL/Redis email validation",
)


class RecordingEmailProvider:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def send_verification_email(self, recipient_email: str, code: str, *, purpose: str):
        self.sent.append({"recipient": recipient_email, "code": code, "purpose": purpose})
        return SimpleNamespace(message_id="integration-message", request_id="integration-request")


def _redis() -> Redis:
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    client.ping()
    return client


def _seed_normal_role(db: DbSession) -> Role:
    role = Role(name="normal", is_enabled=True)
    permission = Permission(
        name="auth:email",
        display_name="Email authentication",
        description="Phase four integration permission",
        is_declared=True,
        is_enabled=True,
    )
    db.add_all([role, permission])
    db.flush()
    db.execute(role_permissions.insert().values(role_id=role.id, permission_id=permission.id))
    db.commit()
    return role


def _assert_session_revoked(db: DbSession, redis: Redis, sid: str) -> None:
    session = db.scalar(select(AuthSession).where(AuthSession.sid == sid))
    assert session is not None
    assert session.status == "revoked"
    assert session.revoked_reason == "password_reset"
    assert session.revoked_at is not None
    key = f"{settings.session_prefix}{sid}"
    assert redis.get(key) == "revoked"
    ttl = redis.ttl(key)
    assert 0 < ttl <= settings.refresh_token_expire_days * 24 * 60 * 60


def test_email_authentication_on_postgres_and_real_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _redis()
    db = SessionLocal()
    provider = RecordingEmailProvider()
    monkeypatch.setattr(session_module, "get_redis", lambda: redis)
    monkeypatch.setattr(refresh_token_module, "get_redis", lambda: redis)
    try:
        monkeypatch.setattr(settings, "email_code_resend_interval_seconds", 1)
        monkeypatch.setattr(settings, "email_send_limit_per_hour", 100)
        monkeypatch.setattr(settings, "email_ip_send_limit_per_minute", 100)
        monkeypatch.setattr(settings, "email_code_max_attempts", 5)
        monkeypatch.setattr(settings, "email_code_expire_minutes", 10)
        inspector = inspect(db.bind)
        user_columns = {column["name"] for column in inspector.get_columns("users")}
        assert "email_verified_at" in user_columns
        assert "email_verified_at" in {column["name"] for column in inspector.get_columns("users")}

        role = _seed_normal_role(db)
        challenges = VerificationChallengeService(redis_client=redis)
        auth = AuthService(db)
        service = EmailAuthService(
            db,
            challenges=challenges,
            email_provider=provider,
            auth=auth,
        )

        registration_code = service.send_registration_code(
            "  New.User@Example.com ", "198.51.100.10"
        )
        created = provider.sent[-1]
        challenge_id = registration_code.challenge_id
        challenge_key = f"{settings.email_challenge_prefix}{challenge_id}"
        assert 0 < redis.ttl(challenge_key) <= registration_code.expires_in
        challenge_values = redis.hgetall(challenge_key)
        assert challenge_values["purpose"] == "register"
        assert "code" not in challenge_values
        assert created["code"] not in str(challenge_values)
        assert "new.user@example.com" not in str(challenge_values)

        registration = service.register(
            " NEW.USER@EXAMPLE.COM ",
            challenge_id,
            created["code"],
            "register-password",
        )
        assert registration.access_token
        assert registration.refresh_token
        registered = db.scalar(select(User).where(User.email == "new.user@example.com"))
        assert registered is not None
        assert registered.email_verified_at is not None
        assert db.scalar(
            select(user_roles.c.role_id).where(
                user_roles.c.user_id == registered.id,
                user_roles.c.role_id == role.id,
            )
        ) == role.id
        assert redis.exists(challenge_key) == 0
        assert provider.sent[0]["purpose"] == "register"

        email_login = service.login("NEW.USER@example.com", "register-password")
        assert email_login.access_token

        time.sleep(settings.email_code_resend_interval_seconds)
        known_forgot = service.send_password_reset_code("new.user@example.com", "198.51.100.11")
        unknown_forgot = service.send_password_reset_code("missing@example.com", "198.51.100.12")
        assert known_forgot.message == unknown_forgot.message
        assert known_forgot.expires_in == unknown_forgot.expires_in
        assert known_forgot.resend_after == unknown_forgot.resend_after
        assert known_forgot.challenge_id != unknown_forgot.challenge_id
        assert provider.sent[-1]["purpose"] == "password_reset"
        assert redis.exists(f"{settings.email_challenge_prefix}{unknown_forgot.challenge_id}") == 0

        old_login = service.login("new.user@example.com", "register-password")
        old_sid = AuthService(db).tokens.verify_access_token(old_login.access_token)["sid"]
        old_refresh_key = f"{settings.refresh_token_prefix}{sha256_text(old_login.refresh_token)}"
        assert redis.exists(old_refresh_key) == 1
        second_sid = "integration-second-session"
        db.add(AuthSession(sid=second_sid, user_id=registered.id, status="active"))
        db.commit()

        reset_code = provider.sent[-1]["code"]
        reset = service.reset_password(
            "new.user@example.com",
            known_forgot.challenge_id,
            reset_code,
            "replacement-password",
        )
        assert reset.message == service.PASSWORD_RESET_MESSAGE
        db.expire_all()
        _assert_session_revoked(db, redis, old_sid)
        _assert_session_revoked(db, redis, second_sid)
        with pytest.raises(ValueError, match="invalid credentials"):
            service.login("new.user@example.com", "register-password")
        assert service.login("new.user@example.com", "replacement-password").access_token

        wrong_attempt = challenges.create_challenge(
            "attempts@example.com", "register", "198.51.100.13"
        )
        wrong_key = f"{settings.email_challenge_prefix}{wrong_attempt.challenge_id}"
        for _ in range(settings.email_code_max_attempts):
            with pytest.raises(ValueError):
                challenges.consume_challenge(
                    wrong_attempt.challenge_id,
                    "attempts@example.com",
                    "register",
                    "000000" if wrong_attempt.code != "000000" else "111111",
                )
        assert redis.exists(wrong_key) == 0

        isolated = challenges.create_challenge(
            "purpose@example.com", "register", "198.51.100.14"
        )
        with pytest.raises(ValueError):
            challenges.consume_challenge(
                isolated.challenge_id,
                "purpose@example.com",
                "password_reset",
                isolated.code,
            )
        assert redis.exists(f"{settings.email_challenge_prefix}{isolated.challenge_id}") == 1

        concurrent = challenges.create_challenge(
            "concurrent@example.com", "register", "198.51.100.15"
        )
        def consume() -> bool:
            try:
                challenges.consume_challenge(
                    concurrent.challenge_id,
                    "concurrent@example.com",
                    "register",
                    concurrent.code,
                )
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(lambda _item: consume(), range(8)))
        assert sum(outcomes) == 1
        assert redis.exists(f"{settings.email_challenge_prefix}{concurrent.challenge_id}") == 0

        limited_email = f"limit-{datetime.now(timezone.utc).timestamp()}@example.com"
        monkeypatch.setattr(settings, "email_send_limit_per_hour", 1)
        limited = challenges.create_challenge(limited_email, "register", "198.51.100.16")
        with pytest.raises(ValueError):
            challenges.create_challenge(limited_email, "register", "198.51.100.16")
        challenges.delete_challenge(limited.challenge_id)
    finally:
        prefix_keys = list(redis.scan_iter(match=f"{settings.redis_key_prefix}*"))
        if prefix_keys:
            redis.delete(*prefix_keys)
        db.close()
        redis.close()
