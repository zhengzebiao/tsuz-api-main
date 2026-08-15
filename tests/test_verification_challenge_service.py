import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.config import Settings
from app.core.security import sha256_text
from app.services.verification_challenge_service import (
    ChallengeAttemptsExceededError,
    ChallengeCodeError,
    ChallengeMismatchError,
    ChallengeNotFoundError,
    ChallengeRateLimitError,
    ChallengeStateError,
    VerificationChallengeService,
)


class FakePipeline:
    def __init__(self, redis: "FakeRedis") -> None:
        self.redis = redis
        self.commands: list[tuple[str, tuple, dict]] = []

    def hset(self, *args, **kwargs):
        self.commands.append(("hset", args, kwargs))
        return self

    def expire(self, *args, **kwargs):
        self.commands.append(("expire", args, kwargs))
        return self

    def execute(self) -> list[object]:
        results = []
        for command, args, kwargs in self.commands:
            results.append(getattr(self.redis, command)(*args, **kwargs))
        return results


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.values: dict[str, int | str] = {}
        self.expirations: dict[str, int] = {}
        self.lock = threading.Lock()
        self.fail_eval = False
        self.fail_pipeline = False

    def pipeline(self, *, transaction: bool = True) -> FakePipeline:
        assert transaction is True
        if self.fail_pipeline:
            raise RuntimeError("redis unavailable")
        return FakePipeline(self)

    def hset(self, key: str, mapping: dict[str, str]) -> int:
        self.hashes.setdefault(key, {}).update({name: str(value) for name, value in mapping.items()})
        return len(mapping)

    def expire(self, key: str, ttl: int) -> bool:
        self.expirations[key] = int(ttl)
        return True

    def delete(self, key: str) -> int:
        removed = int(key in self.hashes or key in self.values)
        self.hashes.pop(key, None)
        self.values.pop(key, None)
        self.expirations.pop(key, None)
        return removed

    def eval(self, script: str, numkeys: int, *args):
        if self.fail_eval:
            raise RuntimeError("redis unavailable")
        keys = list(args[:numkeys])
        argv = list(args[numkeys:])
        with self.lock:
            if "short_created" in script:
                return self._eval_rate_limit(keys, argv)
            return self._eval_consume(keys, argv)

    def _eval_rate_limit(self, keys: list[str], argv: list[object]) -> int:
        short_ttl, hour_ttl, hour_limit, ip_ttl, ip_limit = map(int, argv)
        if keys[0] in self.values:
            return 1
        self.values[keys[0]] = "1"
        self.expirations[keys[0]] = short_ttl

        hourly_count = int(self.values.get(keys[1], 0)) + 1
        self.values[keys[1]] = hourly_count
        self.expirations.setdefault(keys[1], hour_ttl)
        if hourly_count > hour_limit:
            self.values[keys[1]] = hourly_count - 1
            self.delete(keys[0])
            return 2

        ip_count = int(self.values.get(keys[2], 0)) + 1
        self.values[keys[2]] = ip_count
        self.expirations.setdefault(keys[2], ip_ttl)
        if ip_count > ip_limit:
            self.values[keys[2]] = ip_count - 1
            self.values[keys[1]] = hourly_count - 1
            self.delete(keys[0])
            return 3
        return 0

    def _eval_consume(self, keys: list[str], argv: list[object]) -> int:
        key = keys[0]
        purpose, email_hash, code_hash, max_attempts = argv
        data = self.hashes.get(key)
        if not data or data.get("status") != "active":
            return 0
        if data.get("purpose") != purpose or data.get("email_hash") != email_hash:
            return 1

        attempts = int(data.get("attempts", "0"))
        max_attempts = int(max_attempts)
        if attempts >= max_attempts:
            self.delete(key)
            return 3
        if data.get("code_hash") == code_hash:
            self.delete(key)
            return 2

        attempts += 1
        data["attempts"] = str(attempts)
        if attempts >= max_attempts:
            self.delete(key)
            return 3
        return 4


@pytest.fixture
def challenge_settings() -> Settings:
    return Settings(
        _env_file=None,
        email_code_length=6,
        email_code_expire_minutes=10,
        email_code_max_attempts=5,
        email_code_resend_interval_seconds=60,
        email_challenge_prefix="auth:test:email:challenge:",
        email_send_limit_prefix="auth:test:email:send:",
        email_ip_send_limit_prefix="auth:test:email:ip-send:",
        email_send_limit_per_hour=10,
        email_ip_send_limit_per_minute=5,
    )


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


def create_challenge(
    configured_settings: Settings,
    fake_redis: FakeRedis,
    *,
    email: str = "user@example.com",
    purpose: str = "register",
    client_ip: str = "192.0.2.10",
):
    service = VerificationChallengeService(configured_settings, fake_redis)
    created = service.create_challenge(email, purpose, client_ip)
    return service, created


def test_create_challenge_stores_only_hashes_with_ttl(
    challenge_settings: Settings,
    fake_redis: FakeRedis,
) -> None:
    service, created = create_challenge(
        challenge_settings,
        fake_redis,
        email="  User@Example.COM  ",
    )

    assert len(created.code) == 6
    assert created.code.isdigit()
    assert created.expires_in == 600
    assert created.resend_after == 60
    key = f"{challenge_settings.email_challenge_prefix}{created.challenge_id}"
    stored = fake_redis.hashes[key]
    assert stored == {
        "purpose": "register",
        "email_hash": sha256_text("user@example.com"),
        "code_hash": sha256_text(f"{created.challenge_id}:{created.code}"),
        "attempts": "0",
        "status": "active",
    }
    assert fake_redis.expirations[key] == 600
    redis_material = str(fake_redis.hashes)
    assert created.code not in redis_material
    assert "user@example.com" not in redis_material

    rate_material = " ".join(fake_redis.values)
    assert "user@example.com" not in rate_material
    assert "192.0.2.10" not in rate_material
    assert service.normalize_email(" User@Example.COM ") == "user@example.com"


def test_challenge_requires_supported_purpose_and_client_ip(
    challenge_settings: Settings,
    fake_redis: FakeRedis,
) -> None:
    service = VerificationChallengeService(challenge_settings, fake_redis)

    with pytest.raises(ValueError, match="purpose"):
        service.create_challenge("user@example.com", "login", "192.0.2.10")
    with pytest.raises(ValueError, match="client IP"):
        service.create_challenge("user@example.com", "register", "")


def test_correct_code_is_consumed_once(
    challenge_settings: Settings,
    fake_redis: FakeRedis,
) -> None:
    service, created = create_challenge(challenge_settings, fake_redis)

    service.consume_challenge(created.challenge_id, "USER@example.com", "register", created.code)

    with pytest.raises(ChallengeNotFoundError):
        service.consume_challenge(created.challenge_id, "user@example.com", "register", created.code)


def test_purpose_and_email_are_isolated_without_incrementing_attempts(
    challenge_settings: Settings,
    fake_redis: FakeRedis,
) -> None:
    service, created = create_challenge(challenge_settings, fake_redis)
    key = f"{challenge_settings.email_challenge_prefix}{created.challenge_id}"

    with pytest.raises(ChallengeMismatchError):
        service.consume_challenge(created.challenge_id, "user@example.com", "password_reset", created.code)
    assert fake_redis.hashes[key]["attempts"] == "0"

    with pytest.raises(ChallengeMismatchError):
        service.consume_challenge(created.challenge_id, "other@example.com", "register", created.code)
    assert fake_redis.hashes[key]["attempts"] == "0"

    service.consume_challenge(created.challenge_id, "user@example.com", "register", created.code)


def test_wrong_code_increments_attempts_and_fifth_failure_invalidates(
    challenge_settings: Settings,
    fake_redis: FakeRedis,
) -> None:
    service, created = create_challenge(challenge_settings, fake_redis)
    key = f"{challenge_settings.email_challenge_prefix}{created.challenge_id}"

    for attempt in range(1, 5):
        with pytest.raises(ChallengeCodeError):
            service.consume_challenge(created.challenge_id, "user@example.com", "register", "000000")
        assert fake_redis.hashes[key]["attempts"] == str(attempt)

    with pytest.raises(ChallengeAttemptsExceededError):
        service.consume_challenge(created.challenge_id, "user@example.com", "register", "000000")
    assert key not in fake_redis.hashes
    with pytest.raises(ChallengeNotFoundError):
        service.consume_challenge(created.challenge_id, "user@example.com", "register", created.code)


def test_expired_or_deleted_challenge_is_rejected(
    challenge_settings: Settings,
    fake_redis: FakeRedis,
) -> None:
    service, created = create_challenge(challenge_settings, fake_redis)
    service.delete_challenge(created.challenge_id)

    with pytest.raises(ChallengeNotFoundError):
        service.consume_challenge(created.challenge_id, "user@example.com", "register", created.code)


def test_concurrent_consumption_allows_only_one_success(
    challenge_settings: Settings,
    fake_redis: FakeRedis,
) -> None:
    service, created = create_challenge(challenge_settings, fake_redis)
    barrier = threading.Barrier(8)

    def consume() -> str:
        barrier.wait()
        try:
            service.consume_challenge(created.challenge_id, "user@example.com", "register", created.code)
        except ChallengeNotFoundError:
            return "missing"
        return "success"

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _index: consume(), range(8)))

    assert results.count("success") == 1
    assert results.count("missing") == 7


def test_same_email_cannot_resend_within_interval(
    challenge_settings: Settings,
    fake_redis: FakeRedis,
) -> None:
    service, _created = create_challenge(challenge_settings, fake_redis)

    with pytest.raises(ChallengeRateLimitError, match="recently"):
        service.create_challenge("USER@example.com", "password_reset", "192.0.2.11")


def test_email_hourly_limit_and_ip_minute_limit_are_enforced(
    challenge_settings: Settings,
    fake_redis: FakeRedis,
) -> None:
    service = VerificationChallengeService(challenge_settings, fake_redis)
    email = "user@example.com"
    email_hash = sha256_text(email)
    hourly_key = f"{challenge_settings.email_send_limit_prefix}{email_hash}:hour"
    short_key = f"{challenge_settings.email_send_limit_prefix}{email_hash}"
    fake_redis.values[hourly_key] = challenge_settings.email_send_limit_per_hour

    with pytest.raises(ChallengeRateLimitError, match="email"):
        service.create_challenge(email, "register", "192.0.2.20")
    assert short_key not in fake_redis.values
    assert fake_redis.values[hourly_key] == challenge_settings.email_send_limit_per_hour

    other_email = "other@example.com"
    ip = "192.0.2.21"
    ip_hash = sha256_text(ip)
    ip_key = f"{challenge_settings.email_ip_send_limit_prefix}{ip_hash}"
    fake_redis.values[ip_key] = challenge_settings.email_ip_send_limit_per_minute

    with pytest.raises(ChallengeRateLimitError, match="send limit"):
        service.create_challenge(other_email, "register", ip)
    other_hash = sha256_text(other_email)
    assert f"{challenge_settings.email_send_limit_prefix}{other_hash}" not in fake_redis.values
    assert fake_redis.values.get(f"{challenge_settings.email_send_limit_prefix}{other_hash}:hour", 0) == 0


def test_redis_failures_are_converted_to_state_errors(
    challenge_settings: Settings,
    fake_redis: FakeRedis,
) -> None:
    service = VerificationChallengeService(challenge_settings, fake_redis)
    fake_redis.fail_eval = True

    with pytest.raises(ChallengeStateError, match="unavailable"):
        service.create_challenge("user@example.com", "register", "192.0.2.10")

    fake_redis.fail_eval = False
    fake_redis.fail_pipeline = True
    with pytest.raises(ChallengeStateError, match="unavailable"):
        service.create_challenge("other@example.com", "register", "192.0.2.11")
