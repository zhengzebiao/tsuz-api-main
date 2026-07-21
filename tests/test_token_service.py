import pytest
import jwt

from app.core.config import settings
from app.services.token_service import TokenService


def test_create_access_token_includes_required_payload_claims(test_keys: dict[str, str]) -> None:
    token = TokenService().create_access_token(
        user_id="user_123",
        sid="sid_123",
        roles=["admin"],
        scope="user:read",
    )

    payload = jwt.decode(
        token,
        test_keys["public_key"],
        algorithms=[settings.jwt_algorithm],
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
    )

    assert payload["sub"] == "user_123"
    assert payload["sid"] == "sid_123"
    assert payload["roles"] == ["admin"]
    assert payload["scope"] == "user:read"
    assert {"iat", "exp", "jti", "iss", "aud"}.issubset(payload)


def test_verify_access_token_accepts_valid_token() -> None:
    service = TokenService()
    token = service.create_access_token(user_id="user_123", sid="sid_123", roles=["admin"], scope="user:read")

    payload = service.verify_access_token(token)

    assert payload["sub"] == "user_123"
    assert payload["sid"] == "sid_123"


def test_verify_access_token_rejects_invalid_token() -> None:
    with pytest.raises(jwt.PyJWTError):
        TokenService().verify_access_token("not-a-jwt")
