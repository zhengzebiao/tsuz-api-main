from __future__ import annotations

import getpass
import json
import os
import sys
import webbrowser
from collections.abc import Sequence
from urllib.parse import parse_qs, urlparse

import httpx


class RealQQValidationError(RuntimeError):
    """Raised when the guarded real QQ acceptance flow cannot complete."""


SAFE_TEST_HOST_MARKERS = ("localhost", "127.0.0.1", "::1", "test", "staging")
PRODUCTION_HOST_MARKERS = ("prod", "production")


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise RealQQValidationError(message)


def _api_base_url() -> str:
    value = os.getenv("PHASE4_REAL_QQ_API_BASE_URL", "").strip().rstrip("/")
    parsed = urlparse(value)
    _assert(parsed.scheme in {"http", "https"} and parsed.netloc, "real QQ API base URL is invalid")
    host = (parsed.hostname or "").lower()
    _assert(host, "real QQ API base URL has no host")
    _assert(
        any(marker in host for marker in SAFE_TEST_HOST_MARKERS)
        and not any(marker in host for marker in PRODUCTION_HOST_MARKERS),
        "real QQ acceptance only allows a test or local API host",
    )
    return value


def _location_state(location: str) -> bool:
    parsed = urlparse(location)
    return bool(parse_qs(parsed.query).get("state", [""])[0])


def _safe_me_report(body: object) -> dict[str, object]:
    if not isinstance(body, dict):
        raise RealQQValidationError("/auth/me returned an unexpected response")
    identities = body.get("identities")
    identity_providers = []
    if isinstance(identities, list):
        identity_providers = [
            item.get("provider")
            for item in identities
            if isinstance(item, dict) and isinstance(item.get("provider"), str)
        ]
    return {
        "username_is_null": body.get("username") is None,
        "qq_identity_present": "qq" in identity_providers,
        "roles_count": len(body.get("roles", [])) if isinstance(body.get("roles"), list) else 0,
        "permissions_count": (
            len(body.get("permissions", [])) if isinstance(body.get("permissions"), list) else 0
        ),
    }


def run_real_qq_acceptance() -> dict[str, object]:
    if os.getenv("RUN_PHASE4_REAL_QQ") != "1":
        raise RealQQValidationError("set RUN_PHASE4_REAL_QQ=1 to enable real QQ acceptance")
    _assert(os.getenv("APP_ENV", "").strip().lower() == "test", "real QQ acceptance requires APP_ENV=test")
    api_base_url = _api_base_url()

    with httpx.Client(base_url=api_base_url, follow_redirects=False, timeout=20) as client:
        login_response = client.get("/auth/qq/login")
        _assert(login_response.status_code == 302, "QQ login endpoint did not return a redirect")
        authorize_location = login_response.headers.get("location", "")
        _assert(_location_state(authorize_location), "QQ authorization redirect did not contain state")

        if os.getenv("PHASE4_REAL_QQ_SKIP_BROWSER") != "1":
            _assert(webbrowser.open(authorize_location, new=2), "could not open the QQ authorization browser")
        print("QQ authorization browser opened; complete test-app authorization before continuing.")
        input("After the browser reaches the fixed consumer redirect, press Enter. ")
        ticket = getpass.getpass("Paste the one-time ticket (input is hidden): ").strip()
        _assert(ticket, "a QQ ticket is required for exchange")

        exchange_response = client.post("/auth/qq/exchange", json={"ticket": ticket})
        _assert(exchange_response.status_code == 200, "QQ ticket exchange did not succeed")
        exchange_body = exchange_response.json()
        access_token = exchange_body.get("access_token") if isinstance(exchange_body, dict) else None
        _assert(isinstance(access_token, str) and access_token, "QQ exchange returned no access token")

        me_response = client.get("/auth/me", headers={"Authorization": f"Bearer {access_token}"})
        _assert(me_response.status_code == 200, "the exchanged Bearer token failed /auth/me")
        me_report = _safe_me_report(me_response.json())
        _assert(me_report["qq_identity_present"], "the authenticated user has no QQ identity")

        replay_response = client.post("/auth/qq/exchange", json={"ticket": ticket})
        _assert(replay_response.status_code == 401, "the QQ ticket was reusable")

    return {
        "authorization_redirect": True,
        "ticket_exchange": True,
        "bearer_me": me_report,
        "ticket_replay_rejected": True,
        "sensitive_values_reported": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    if os.getenv("RUN_PHASE4_REAL_QQ") != "1":
        print("Set RUN_PHASE4_REAL_QQ=1 to run guarded real QQ acceptance.", file=sys.stderr)
        return 2
    try:
        report = run_real_qq_acceptance()
    except (OSError, ValueError, httpx.HTTPError, RealQQValidationError) as exc:
        print(f"[FAIL] real QQ acceptance: {exc}", file=sys.stderr)
        return 1
    print(f"[PASS] real QQ acceptance: {json.dumps(report, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
