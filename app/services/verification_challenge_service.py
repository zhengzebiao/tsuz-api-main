from __future__ import annotations

import logging
import secrets
import string
from dataclasses import dataclass

from redis import Redis

from app.core.config import Settings, settings
from app.core.redis import get_redis
from app.core.security import sha256_text

logger = logging.getLogger("app.auth.challenge")


class ChallengeError(ValueError):
    """Base class for verification challenge failures."""


class ChallengeRateLimitError(ChallengeError):
    """The email or client has exceeded a send limit."""


class ChallengeNotFoundError(ChallengeError):
    """The challenge is missing, expired, or already consumed."""


class ChallengeMismatchError(ChallengeError):
    """The challenge purpose or email does not match the request."""


class ChallengeCodeError(ChallengeError):
    """The submitted code is incorrect."""


class ChallengeAttemptsExceededError(ChallengeError):
    """The challenge reached its maximum number of failed attempts."""


class ChallengeStateError(RuntimeError):
    """Redis could not read or update challenge state."""


@dataclass(frozen=True)
class CreatedChallenge:
    challenge_id: str
    code: str
    expires_in: int
    resend_after: int


class VerificationChallengeService:
    """Store and atomically consume short-lived email verification challenges."""

    _PURPOSES = frozenset({"register", "password_reset"})
    _RATE_LIMIT_SCRIPT = """
local short_created = redis.call('SET', KEYS[1], '1', 'NX', 'EX', ARGV[1])
if not short_created then
    return 1
end

local hourly_count = redis.call('INCR', KEYS[2])
if hourly_count == 1 then
    redis.call('EXPIRE', KEYS[2], ARGV[2])
end
if hourly_count > tonumber(ARGV[3]) then
    redis.call('DECR', KEYS[2])
    redis.call('DEL', KEYS[1])
    return 2
end

local ip_created = redis.call('INCR', KEYS[3])
if ip_created == 1 then
    redis.call('EXPIRE', KEYS[3], ARGV[4])
end
if ip_created > tonumber(ARGV[5]) then
    redis.call('DECR', KEYS[3])
    redis.call('DECR', KEYS[2])
    redis.call('DEL', KEYS[1])
    return 3
end
return 0
"""

    _CONSUME_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 0 then
    return 0
end
if redis.call('HGET', KEYS[1], 'status') ~= 'active' then
    return 0
end
if redis.call('HGET', KEYS[1], 'purpose') ~= ARGV[1]
    or redis.call('HGET', KEYS[1], 'email_hash') ~= ARGV[2] then
    return 1
end

local attempts = tonumber(redis.call('HGET', KEYS[1], 'attempts') or '0')
local max_attempts = tonumber(ARGV[4])
if attempts >= max_attempts then
    redis.call('DEL', KEYS[1])
    return 3
end
if redis.call('HGET', KEYS[1], 'code_hash') == ARGV[3] then
    redis.call('DEL', KEYS[1])
    return 2
end

attempts = redis.call('HINCRBY', KEYS[1], 'attempts', 1)
if attempts >= max_attempts then
    redis.call('DEL', KEYS[1])
    return 3
end
return 4
"""

    def __init__(
        self,
        configured_settings: Settings | None = None,
        redis_client: Redis | None = None,
    ) -> None:
        self.settings = configured_settings or settings
        self.redis = redis_client if redis_client is not None else get_redis()

    def create_challenge(self, email: str, purpose: str, client_ip: str) -> CreatedChallenge:
        normalized_email = self.normalize_email(email)
        self._validate_purpose(purpose)
        if not client_ip:
            raise ValueError("client IP is required")

        rate_result = self._check_rate_limits(normalized_email, client_ip)
        if rate_result:
            raise ChallengeRateLimitError(self._rate_limit_message(rate_result))

        challenge_id = secrets.token_urlsafe(32)
        code = "".join(secrets.choice(string.digits) for _ in range(self.settings.email_code_length))
        key = self._challenge_key(challenge_id)
        try:
            pipeline = self.redis.pipeline(transaction=True)
            pipeline.hset(
                key,
                mapping={
                    "purpose": purpose,
                    "email_hash": sha256_text(normalized_email),
                    "code_hash": sha256_text(f"{challenge_id}:{code}"),
                    "attempts": "0",
                    "status": "active",
                },
            )
            pipeline.expire(key, self.settings.email_code_expire_minutes * 60)
            pipeline.execute()
        except Exception as exc:
            logger.error("challenge storage failed purpose=%s", purpose)
            raise ChallengeStateError("verification state unavailable") from exc

        return CreatedChallenge(
            challenge_id=challenge_id,
            code=code,
            expires_in=self.settings.email_code_expire_minutes * 60,
            resend_after=self.settings.email_code_resend_interval_seconds,
        )

    def consume_challenge(self, challenge_id: str, email: str, purpose: str, code: str) -> None:
        self._validate_purpose(purpose)
        if not challenge_id or not code:
            raise ChallengeNotFoundError("verification challenge is invalid")
        normalized_email = self.normalize_email(email)
        key = self._challenge_key(challenge_id)
        try:
            result = self.redis.eval(
                self._CONSUME_SCRIPT,
                1,
                key,
                purpose,
                sha256_text(normalized_email),
                sha256_text(f"{challenge_id}:{code}"),
                self.settings.email_code_max_attempts,
            )
        except Exception as exc:
            logger.error("challenge consumption failed purpose=%s", purpose)
            raise ChallengeStateError("verification state unavailable") from exc

        if result == 2:
            return
        if result == 1:
            raise ChallengeMismatchError("verification challenge does not match")
        if result == 3:
            raise ChallengeAttemptsExceededError("verification challenge attempts exceeded")
        if result == 4:
            raise ChallengeCodeError("invalid verification code")
        raise ChallengeNotFoundError("verification challenge is invalid or expired")

    def delete_challenge(self, challenge_id: str) -> None:
        if not challenge_id:
            return
        try:
            self.redis.delete(self._challenge_key(challenge_id))
        except Exception as exc:
            logger.warning("challenge cleanup failed")
            raise ChallengeStateError("verification state unavailable") from exc

    @staticmethod
    def normalize_email(email: str) -> str:
        return str(email).strip().lower()

    def _check_rate_limits(self, email: str, client_ip: str) -> int:
        email_hash = sha256_text(email)
        ip_hash = sha256_text(client_ip)
        email_prefix = self.settings.email_send_limit_prefix
        keys = [
            f"{email_prefix}{email_hash}",
            f"{email_prefix}{email_hash}:hour",
            f"{self.settings.email_ip_send_limit_prefix}{ip_hash}",
        ]
        try:
            return int(
                self.redis.eval(
                    self._RATE_LIMIT_SCRIPT,
                    len(keys),
                    *keys,
                    self.settings.email_code_resend_interval_seconds,
                    60 * 60,
                    self.settings.email_send_limit_per_hour,
                    60,
                    self.settings.email_ip_send_limit_per_minute,
                )
                or 0
            )
        except Exception as exc:
            logger.error("challenge rate limit check failed")
            raise ChallengeStateError("verification state unavailable") from exc

    def _challenge_key(self, challenge_id: str) -> str:
        return f"{self.settings.email_challenge_prefix}{challenge_id}"

    def _validate_purpose(self, purpose: str) -> None:
        if purpose not in self._PURPOSES:
            raise ValueError("invalid verification challenge purpose")

    def _rate_limit_message(self, result: int) -> str:
        if result == 1:
            return "verification code sent too recently"
        if result == 2:
            return "email verification send limit exceeded"
        return "verification send limit exceeded"
