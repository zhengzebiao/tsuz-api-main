from datetime import datetime, timezone

import pytest

import app.services.blacklist_service as blacklist_module
import app.services.session_service as session_module
from app.core.config import settings
from app.services.blacklist_service import BlacklistService
from app.services.session_service import SessionService


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    def setex(self, key: str, ttl: int, value: str) -> None:
        self.values[key] = value
        self.expirations[key] = ttl

    def exists(self, key: str) -> bool:
        return key in self.values

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def get(self, key: str) -> str | None:
        return self.values.get(key)


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    redis = FakeRedis()
    monkeypatch.setattr(blacklist_module, "get_redis", lambda: redis)
    monkeypatch.setattr(session_module, "get_redis", lambda: redis)
    return redis


def test_blacklist_service_uses_configured_prefix_and_access_token_ttl(fake_redis: FakeRedis) -> None:
    exp = int(datetime.now(timezone.utc).timestamp()) + 120

    BlacklistService().add_jti("jti-123", exp)

    key = f"{settings.token_blacklist_prefix}jti-123"
    assert fake_redis.values[key] == "1"
    assert 1 <= fake_redis.expirations[key] <= 120
    with pytest.raises(ValueError, match="blacklisted"):
        BlacklistService().ensure_not_blacklisted("jti-123")


def test_session_service_uses_configured_prefix_for_revocation(fake_redis: FakeRedis) -> None:
    SessionService().revoke_session("sid-123")

    assert fake_redis.values[f"{settings.session_prefix}sid-123"] == "revoked"
    with pytest.raises(ValueError, match="session is revoked"):
        SessionService().ensure_session_active("sid-123")
