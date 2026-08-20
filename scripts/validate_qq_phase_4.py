from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from redis import Redis
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app.core.security import sha256_text
from scripts.validate_phase_4 import (
    Phase4Config,
    Phase4ValidationError,
    _alembic,
    _clean_redis_namespace,
    _database_secrets,
    _redact,
    _runtime_env,
    _seed,
    _stop_process,
    _sync_permissions,
    _wait_for_api,
    temporary_postgres_database,
)

ROOT_DIR = Path(__file__).resolve().parents[1]


class FakeQQProviderState:
    def __init__(self) -> None:
        self.app_id = f"phase4-{secrets.token_hex(8)}"
        self.app_key = secrets.token_urlsafe(32)
        self.access_token = secrets.token_urlsafe(32)
        self.openid = secrets.token_urlsafe(24)
        self.authorization_code = secrets.token_urlsafe(24)
        self.failure = False
        self.redirect_uris: list[str] = []
        self._lock = threading.Lock()

    def record_redirect_uri(self, value: str) -> None:
        with self._lock:
            self.redirect_uris.append(value)


class FakeQQProviderHandler(BaseHTTPRequestHandler):
    state: FakeQQProviderState

    def do_GET(self) -> None:
        if self.state.failure:
            self._write(503, "provider unavailable", "text/plain")
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/token":
            redirect_uri = query.get("redirect_uri", [""])[0]
            self.state.record_redirect_uri(redirect_uri)
            self._write(200, urlencode({"access_token": self.state.access_token, "expires_in": "60"}))
            return
        if parsed.path == "/openid":
            self._write(
                200,
                f"callback({{\"client_id\":\"{self.state.app_id}\",\"openid\":\"{self.state.openid}\"}});",
            )
            return
        if parsed.path == "/profile":
            self._write(
                200,
                json.dumps({"ret": 0, "nickname": "Phase Four QQ", "figureurl_qq_2": "https://avatar.invalid/qq"}),
                "application/json",
            )
            return
        self._write(404, "not found", "text/plain")

    def _write(self, status: int, body: str, content_type: str = "application/x-www-form-urlencoded") -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _start_provider() -> tuple[HTTPServer, FakeQQProviderState, threading.Thread, str]:
    state = FakeQQProviderState()

    class Handler(FakeQQProviderHandler):
        pass

    Handler.state = state
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, state, thread, f"http://127.0.0.1:{server.server_port}"


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise Phase4ValidationError(message)


def _redirect(response: httpx.Response, expected_status: int = 302) -> str:
    _assert(response.status_code == expected_status, f"unexpected redirect status {response.status_code}")
    location = response.headers.get("location")
    _assert(isinstance(location, str) and location, "redirect did not include a location")
    return location


def _state_from_location(location: str) -> str:
    state = parse_qs(urlparse(location).query).get("state", [""])[0]
    _assert(bool(state), "authorization redirect did not include state")
    return state


def _ticket_from_location(location: str) -> str:
    ticket = parse_qs(urlparse(location).query).get("ticket", [""])[0]
    _assert(bool(ticket), "consumer redirect did not include ticket")
    return ticket


def _session_count(engine: Any, user_id: int) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text("SELECT count(*) FROM sessions WHERE user_id = :user_id"),
                {"user_id": user_id},
            ).scalar_one()
        )


def _user_id_for_qq(engine: Any) -> int:
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT id FROM users WHERE email IS NULL ORDER BY id DESC LIMIT 1")
        ).scalar_one_or_none()
    _assert(isinstance(value, int), "QQ-only user was not created")
    return value


def _qq_settings_env(
    database_url: str,
    redis_url: str,
    prefix: str,
    api_port: int,
    provider_url: str,
    provider: FakeQQProviderState,
) -> dict[str, str]:
    env = _runtime_env(database_url, redis_url, prefix)
    env.update(
        {
            "APP_ENV": "test",
            "APP_ID": provider.app_id,
            "APP_KEY": provider.app_key,
            "QQ_REDIRECT_URI": f"http://127.0.0.1:{api_port}/auth/qq/callback",
            "QQ_TICKET_REDIRECT_URI": "https://consumer.phase4.test/login",
            "QQ_AUTHORIZE_URL": f"{provider_url}/authorize",
            "QQ_TOKEN_URL": f"{provider_url}/token",
            "QQ_OPENID_URL": f"{provider_url}/openid",
            "QQ_USER_INFO_URL": f"{provider_url}/profile",
            "QQ_STATE_PREFIX": f"{prefix}qq:state:",
            "QQ_TICKET_PREFIX": f"{prefix}qq:ticket:",
            "QQ_STATE_TTL_SECONDS": "300",
            "QQ_TICKET_TTL_SECONDS": "60",
            "QQ_HTTP_TIMEOUT_SECONDS": "5",
        }
    )
    return env


def run_qq_integration(config: Phase4Config) -> dict[str, Any]:
    server, provider, provider_thread, provider_url = _start_provider()
    process: subprocess.Popen[str] | None = None
    api_log = tempfile.NamedTemporaryFile(mode="w+t", encoding="utf-8")  # noqa: SIM115
    try:
        with temporary_postgres_database(config, "qq") as database:
            suffix = database.name.rsplit("_", 1)[-1]
            redis_prefix = f"auth:phase4-qq:{suffix}:"
            redis_client: Redis = Redis.from_url(config.redis_url, decode_responses=True)
            redis_client.ping()
            _clean_redis_namespace(redis_client, redis_prefix)
            port = int(os.getenv("PHASE4_QQ_API_PORT", "0"))
            if not 1 <= port <= 65535:
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = int(sock.getsockname()[1])
            env = _qq_settings_env(database.url, config.redis_url, redis_prefix, port, provider_url, provider)
            _alembic(database.url, "upgrade", "head")
            _seed(env)
            _sync_permissions(env)
            api_base = f"http://127.0.0.1:{port}"
            process = subprocess.Popen(
                (sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)),
                cwd=ROOT_DIR,
                env=env,
                text=True,
                stdout=api_log,
                stderr=subprocess.STDOUT,
            )
            engine = create_engine(database.url, poolclass=NullPool)
            try:
                _wait_for_api(api_base, process)
                with httpx.Client(base_url=api_base, follow_redirects=False, timeout=15) as client:
                    login_location = _redirect(client.get("/auth/qq/login"))
                    login_query = parse_qs(urlparse(login_location).query)
                    state = _state_from_location(login_location)
                    _assert(login_query.get("redirect_uri") == [env["QQ_REDIRECT_URI"]], "provider callback was not fixed")
                    state_key = f'{env["QQ_STATE_PREFIX"]}{sha256_text(state)}'
                    _assert(redis_client.get(state_key) == "pending", "state was not stored as a hashed pending value")
                    _assert(0 < redis_client.ttl(state_key) <= 300, "state TTL was not bounded")

                    callback = client.get("/auth/qq/callback", params={"code": provider.authorization_code, "state": state})
                    ticket_location = _redirect(callback)
                    _assert(ticket_location.startswith(env["QQ_TICKET_REDIRECT_URI"]), "consumer redirect was not fixed")
                    ticket = _ticket_from_location(ticket_location)
                    _assert(redis_client.get(state_key) is None, "state was reusable")
                    ticket_key = f'{env["QQ_TICKET_PREFIX"]}{sha256_text(ticket)}'
                    _assert(0 < redis_client.ttl(ticket_key) <= 60, "ticket TTL was not bounded")
                    user_id = _user_id_for_qq(engine)
                    _assert(_session_count(engine, user_id) == 0, "exchange created a session too early")

                    exchanged = client.post("/auth/qq/exchange", json={"ticket": ticket})
                    _assert(exchanged.status_code == 200, "ticket exchange failed")
                    token_body = exchanged.json()
                    access_token = token_body.get("access_token")
                    _assert(isinstance(access_token, str) and access_token, "exchange did not return an access token")
                    _assert(_session_count(engine, user_id) == 1, "exchange did not create one session")
                    me = client.get("/auth/me", headers={"Authorization": f"Bearer {access_token}"})
                    _assert(me.status_code == 200, "Bearer token could not call /auth/me")
                    me_body = me.json()
                    _assert(me_body["username"] is None, "QQ-only username was not null")
                    _assert(me_body["identities"][0]["provider"] == "qq", "safe identity was missing")
                    _assert("provider_subject" not in json.dumps(me_body), "provider subject leaked from /auth/me")
                    _assert(client.post("/auth/qq/exchange", json={"ticket": ticket}).status_code == 401, "ticket replay succeeded")

                    stale_ticket = "phase4-expired-ticket"
                    stale_key = f'{env["QQ_TICKET_PREFIX"]}{sha256_text(stale_ticket)}'
                    redis_client.set(stale_key, str(user_id), ex=1)
                    time.sleep(1.2)
                    _assert(client.post("/auth/qq/exchange", json={"ticket": stale_ticket}).status_code == 401, "expired ticket was accepted")

                    stale_login = _redirect(client.get("/auth/qq/login"))
                    stale_state = _state_from_location(stale_login)
                    redis_client.delete(f'{env["QQ_STATE_PREFIX"]}{sha256_text(stale_state)}')
                    stale_callback = client.get("/auth/qq/callback", params={"code": provider.authorization_code, "state": stale_state})
                    _assert("qq_error=oauth_failed" in _redirect(stale_callback), "expired state did not use stable error redirect")

                    with engine.begin() as connection:
                        connection.execute(text("UPDATE users SET is_active = false WHERE id = :id"), {"id": user_id})
                    disabled_login = _redirect(client.get("/auth/qq/login"))
                    disabled_callback = client.get(
                        "/auth/qq/callback",
                        params={"code": provider.authorization_code, "state": _state_from_location(disabled_login)},
                    )
                    _assert("qq_error=oauth_failed" in _redirect(disabled_callback), "disabled user callback was not rejected")
                    with engine.begin() as connection:
                        connection.execute(text("UPDATE users SET is_active = true, is_blacklisted = true WHERE id = :id"), {"id": user_id})
                    blacklisted_login = _redirect(client.get("/auth/qq/login"))
                    blacklisted_callback = client.get(
                        "/auth/qq/callback",
                        params={"code": provider.authorization_code, "state": _state_from_location(blacklisted_login)},
                    )
                    _assert("qq_error=oauth_failed" in _redirect(blacklisted_callback), "blacklisted user callback was not rejected")
                    with engine.begin() as connection:
                        connection.execute(text("UPDATE users SET is_blacklisted = false WHERE id = :id"), {"id": user_id})

                    provider.failure = True
                    provider_login = _redirect(client.get("/auth/qq/login"))
                    provider_callback = client.get(
                        "/auth/qq/callback",
                        params={"code": provider.authorization_code, "state": _state_from_location(provider_login)},
                    )
                    _assert("qq_error=oauth_failed" in _redirect(provider_callback), "provider failure was not stable")
                    provider.failure = False
                    _assert(provider.redirect_uris == [env["QQ_REDIRECT_URI"]], "provider received an unexpected callback URL")
            finally:
                engine.dispose()
                _stop_process(process)
                _clean_redis_namespace(redis_client, redis_prefix)
                redis_client.close()
    finally:
        if process is not None:
            _stop_process(process)
        api_log.flush()
        api_log.seek(0)
        log_text = api_log.read()
        api_log.close()
        server.shutdown()
        server.server_close()
        provider_thread.join(timeout=5)
        for sensitive in (provider.authorization_code, provider.access_token, provider.openid):
            _assert(sensitive not in log_text, "QQ provider value appeared in API logs")

    return {
        "authorization": True,
        "callback": True,
        "fixed_consumer": True,
        "ticket_exchange": True,
        "bearer_me": True,
        "replay_and_expiry": True,
        "disabled_and_blacklisted": True,
        "provider_failure": True,
        "temporary_resources_cleaned": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate the QQ OAuth route flow with isolated PostgreSQL/Redis and a fake provider")
    parser.add_argument("--only", choices=("qq",), default="qq")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if os.getenv("RUN_PHASE4_QQ_INTEGRATION") != "1":
        print("Set RUN_PHASE4_QQ_INTEGRATION=1 to run isolated QQ integration.", file=sys.stderr)
        return 2
    try:
        report = run_qq_integration(Phase4Config.from_env())
    except (OSError, ValueError, Phase4ValidationError) as exc:
        safe_error = _redact(str(exc), _database_secrets(os.getenv("PHASE4_ADMIN_DATABASE_URL", "")))
        print(f"[FAIL] QQ phase 4 validation: {safe_error}", file=sys.stderr)
        return 1
    print(f"[PASS] QQ phase 4 validation: {json.dumps(report, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
