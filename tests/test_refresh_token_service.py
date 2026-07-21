from datetime import datetime, timedelta, timezone
import logging

import pytest

import app.services.refresh_token_service as refresh_module
import app.services.session_service as session_module
from app.core.config import settings
from app.core.security import sha256_text
from app.services.refresh_token_service import RefreshTokenReuseError, RefreshTokenService


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    def hset(self, key: str, mapping: dict[str, str]) -> None:
        self.hashes.setdefault(key, {}).update(mapping)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def expire(self, key: str, ttl: int) -> None:
        self.expirations[key] = ttl

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def get(self, key: str) -> str | None:
        return self.values.get(key)


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    redis = FakeRedis()
    monkeypatch.setattr(refresh_module, "get_redis", lambda: redis)
    monkeypatch.setattr(session_module, "get_redis", lambda: redis)
    return redis


def refresh_key(refresh_token: str) -> str:
    return f"{settings.refresh_token_prefix}{sha256_text(refresh_token)}"


def session_key(sid: str) -> str:
    return f"{settings.session_prefix}{sid}"


def test_rotate_refresh_token_marks_old_hash_and_returns_new_token(fake_redis: FakeRedis) -> None:
    service = RefreshTokenService()
    refresh_token = service.create_refresh_token(user_id="user_123", sid="sid_123")

    rotation = service.rotate_refresh_token(refresh_token)

    old_hash = fake_redis.hgetall(refresh_key(refresh_token))
    new_hash = fake_redis.hgetall(refresh_key(rotation["refresh_token"]))
    assert rotation["user_id"] == "user_123"
    assert rotation["sid"] == "sid_123"
    assert rotation["refresh_token"] != refresh_token
    assert old_hash["status"] == "rotated"
    assert old_hash["rotated_at"]
    assert old_hash["replaced_by"] == sha256_text(rotation["refresh_token"])
    assert new_hash["status"] == "active"
    assert new_hash["created_at"]


def test_refresh_token_plaintext_is_not_stored_in_redis(fake_redis: FakeRedis) -> None:
    service = RefreshTokenService()
    refresh_token = service.create_refresh_token(user_id="user_123", sid="sid_123")
    rotation = service.rotate_refresh_token(refresh_token)
    redis_material = " ".join(
        [*fake_redis.hashes.keys(), *[str(value) for item in fake_redis.hashes.values() for value in item.values()]]
    )

    assert refresh_token not in redis_material
    assert rotation["refresh_token"] not in redis_material
    assert sha256_text(refresh_token) in redis_material
    assert sha256_text(rotation["refresh_token"]) in redis_material


def test_rotated_refresh_token_within_grace_does_not_revoke_session(fake_redis: FakeRedis) -> None:
    service = RefreshTokenService()
    refresh_token = service.create_refresh_token(user_id="user_123", sid="sid_123")
    service.rotate_refresh_token(refresh_token)

    with pytest.raises(ValueError, match="already rotated"):
        service.rotate_refresh_token(refresh_token)

    assert fake_redis.get(session_key("sid_123")) is None


def test_rotated_refresh_token_after_grace_revokes_session(fake_redis: FakeRedis, caplog) -> None:
    service = RefreshTokenService()
    refresh_token = service.create_refresh_token(user_id="user_123", sid="sid_123")
    service.rotate_refresh_token(refresh_token)
    fake_redis.hashes[refresh_key(refresh_token)]["rotated_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=settings.refresh_token_reuse_grace_seconds + 1)
    ).isoformat()

    with caplog.at_level(logging.WARNING, logger="app.auth.refresh"):
        with pytest.raises(RefreshTokenReuseError, match="reuse detected"):
            service.rotate_refresh_token(refresh_token)

    assert fake_redis.get(session_key("sid_123")) == "revoked"
    assert "refresh reuse detected" in caplog.text
    assert refresh_token not in caplog.text


def test_revoked_session_rejects_refresh(fake_redis: FakeRedis) -> None:
    service = RefreshTokenService()
    refresh_token = service.create_refresh_token(user_id="user_123", sid="sid_123")
    service.revoke_session("sid_123")

    with pytest.raises(ValueError, match="session is revoked"):
        service.rotate_refresh_token(refresh_token)
