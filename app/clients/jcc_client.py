import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger("app.jcc_client")


class JccClientError(RuntimeError):
    code = "JCC_CLIENT_ERROR"


class JccClientConfigurationError(JccClientError):
    code = "JCC_CLIENT_CONFIGURATION_ERROR"


class JccClientAuthenticationError(JccClientError):
    code = "JCC_CLIENT_AUTHENTICATION_ERROR"


class JccClientRequestError(JccClientError):
    code = "JCC_CLIENT_REQUEST_ERROR"


@dataclass(frozen=True)
class CachedServiceToken:
    value: str
    refresh_at: float


class JccClient:
    REQUIRED_SCOPE = "jcc:record:read"
    STATS_SCOPE = "jcc:stats:read"
    REFRESH_MARGIN_SECONDS = 30

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=settings.internal_http_timeout_seconds)
        self._owns_client = client is None
        self._cached_tokens: dict[str, CachedServiceToken] = {}
        self._cache_lock = threading.Lock()

    def list_records(self, *, request_id: str | None = None) -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {self._service_token(self.REQUIRED_SCOPE)}"}
        if request_id:
            headers["X-Request-ID"] = request_id
        try:
            response = self._client.get(
                f"{settings.jcc_api_base_url.rstrip('/')}/internal/v1/records",
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("JCC request failed reason=upstream_error")
            raise JccClientRequestError(JccClientRequestError.code) from exc
        if not isinstance(payload, list):
            raise JccClientRequestError(JccClientRequestError.code)
        return payload

    def get_resource_statistics(self, *, request_id: str | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._service_token(self.STATS_SCOPE)}"}
        if request_id:
            headers["X-Request-ID"] = request_id
        try:
            response = self._client.get(
                f"{settings.jcc_api_base_url.rstrip('/')}/internal/v1/resource-statistics",
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("JCC resource statistics request failed reason=upstream_error")
            raise JccClientRequestError(JccClientRequestError.code) from exc
        if not isinstance(payload, dict):
            raise JccClientRequestError(JccClientRequestError.code)
        return payload

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _service_token(self, scope: str) -> str:
        now = time.monotonic()
        cached = self._cached_tokens.get(scope)
        if cached is not None and cached.refresh_at > now:
            return cached.value
        with self._cache_lock:
            cached = self._cached_tokens.get(scope)
            now = time.monotonic()
            if cached is not None and cached.refresh_at > now:
                return cached.value
            token = self._fetch_service_token(now, scope)
            self._cached_tokens[scope] = token
            return token.value

    def _fetch_service_token(self, now: float, scope: str) -> CachedServiceToken:
        if not settings.main_app_id or not settings.main_app_secret or not settings.jcc_app_id:
            raise JccClientConfigurationError(JccClientConfigurationError.code)
        try:
            response = self._client.post(
                settings.main_token_url,
                auth=httpx.BasicAuth(settings.main_app_id, settings.main_app_secret),
                data={
                    "grant_type": "client_credentials",
                    "audience": settings.jcc_app_id,
                    "scope": scope,
                },
            )
            response.raise_for_status()
            payload = response.json()
            token = payload["access_token"]
            expires_in = payload["expires_in"]
            if not isinstance(token, str) or not token:
                raise ValueError("invalid token response")
            if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in <= 0:
                raise ValueError("invalid expiry response")
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            logger.warning("JCC token request failed reason=authentication_error")
            raise JccClientAuthenticationError(JccClientAuthenticationError.code) from exc
        refresh_in = max(0, expires_in - self.REFRESH_MARGIN_SECONDS)
        return CachedServiceToken(token, now + refresh_in)
