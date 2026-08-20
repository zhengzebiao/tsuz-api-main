import re
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.core.security import (
    generate_app_id,
    generate_app_secret,
    hash_app_secret,
    verify_app_secret,
)
from app.models.app import App
from app.schemas.admin_app import (
    AdminAppActionResponse,
    AdminAppCreate,
    AdminAppCreateResponse,
    AdminAppDisableRequest,
    AdminAppRegenerateSecretRequest,
    AdminAppResponse,
    AdminAppSecretResponse,
    AdminAppUpdate,
)


def app_for_response() -> App:
    now = datetime(2026, 8, 12, 10, 30, tzinfo=UTC).replace(tzinfo=None)
    return App(
        id=1,
        app_id="app_2d92f64361ea4e249f5c9a0de38bc092",
        app_secret_hash="a" * 64,
        name="Project Management",
        icon_url="https://static.example.com/project.png",
        access_url="https://project.example.com",
        service_account_name="Project Management Service",
        is_enabled=True,
        secret_updated_at=now,
        created_at=now,
        updated_at=now,
        version=1,
    )


def test_generate_app_id_has_expected_format_and_is_random() -> None:
    app_ids = {generate_app_id() for _ in range(100)}

    assert len(app_ids) == 100
    assert all(re.fullmatch(r"app_[0-9a-f]{32}", app_id) for app_id in app_ids)


def test_generate_app_secret_is_high_entropy_and_random() -> None:
    secrets = {generate_app_secret() for _ in range(100)}

    assert len(secrets) == 100
    assert all(secret.startswith("app_secret_") for secret in secrets)
    assert all(len(secret) > 50 for secret in secrets)


def test_app_secret_hash_can_be_verified_without_storing_plaintext() -> None:
    app_secret = generate_app_secret()
    app_secret_hash = hash_app_secret(app_secret)

    assert len(app_secret_hash) == 64
    assert app_secret not in app_secret_hash
    assert verify_app_secret(app_secret, app_secret_hash) is True
    assert verify_app_secret("app_secret_wrong", app_secret_hash) is False
    assert verify_app_secret(app_secret, hash_app_secret("app_secret_old")) is False


def test_create_schema_normalizes_text_and_urls() -> None:
    payload = AdminAppCreate(
        name="  Project Management  ",
        icon_url="https://static.example.com/project.png",
        access_url="https://project.example.com",
        service_account_name="  Project Management Service  ",
    )

    assert payload.name == "Project Management"
    assert payload.icon_url == "https://static.example.com/project.png"
    assert payload.access_url == "https://project.example.com/"
    assert payload.service_account_name == "Project Management Service"


def test_update_schema_requires_version_and_allows_clearing_icon() -> None:
    payload = AdminAppUpdate(name="  New Name ", icon_url=None, version=1)

    assert payload.name == "New Name"
    assert payload.icon_url is None
    assert payload.version == 1

    with pytest.raises(ValidationError):
        AdminAppUpdate(name="New Name", version=0)


def test_schema_rejects_invalid_urls_blank_text_and_sensitive_fields() -> None:
    invalid_payloads = (
        {"name": "Name", "access_url": "ftp://example.com", "service_account_name": "Service"},
        {"name": "   ", "access_url": "https://example.com", "service_account_name": "Service"},
        {"name": "Name", "access_url": "https://example.com", "service_account_name": "   "},
        {
            "name": "Name",
            "access_url": "https://example.com",
            "service_account_name": "Service",
            "app_secret": "should-not-be-accepted",
        },
        {
            "name": "Name",
            "access_url": "https://example.com",
            "service_account_name": "Service",
            "app_secret_hash": "a" * 64,
        },
    )

    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            AdminAppCreate(**payload)


def test_disable_and_regenerate_secret_requests_normalize_reasons() -> None:
    assert AdminAppDisableRequest(reason="  maintenance  ").reason == "maintenance"
    assert AdminAppDisableRequest(reason="   ").reason is None
    assert AdminAppDisableRequest().reason is None
    assert AdminAppRegenerateSecretRequest(reason="  possible leak  ").reason == "possible leak"

    with pytest.raises(ValidationError):
        AdminAppRegenerateSecretRequest(reason="   ")


def test_normal_app_responses_exclude_secret_and_hash() -> None:
    app = app_for_response()
    response = AdminAppResponse.model_validate(app)
    action_response = AdminAppActionResponse(**response.model_dump(), changed=True)

    assert "app_secret" not in response.model_dump()
    assert "app_secret_hash" not in response.model_dump()
    assert "app_secret" not in action_response.model_dump()
    assert "app_secret_hash" not in action_response.model_dump()
    with pytest.raises(ValidationError):
        AdminAppResponse(**response.model_dump(), app_secret_hash=app.app_secret_hash)
    with pytest.raises(ValidationError):
        AdminAppActionResponse(**response.model_dump(), changed=True, app_secret="forbidden")


def test_secret_is_only_present_in_explicit_one_time_responses() -> None:
    app = app_for_response()
    app_response = AdminAppResponse.model_validate(app)
    app_secret = generate_app_secret()
    created = AdminAppCreateResponse(app=app_response, app_secret=app_secret)
    regenerated = AdminAppSecretResponse(
        app_id=app.app_id,
        app_secret=app_secret,
        secret_updated_at=app.secret_updated_at,
    )

    assert created.app.app_id == app.app_id
    assert created.app_secret == app_secret
    assert regenerated.app_secret == app_secret
    assert "app_secret_hash" not in created.model_dump_json()
    assert "app_secret_hash" not in regenerated.model_dump_json()
