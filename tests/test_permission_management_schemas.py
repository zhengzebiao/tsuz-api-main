from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.models.permission import Permission
from app.models.permission_endpoint import PermissionEndpoint
from app.schemas.admin_permission import (
    AdminPermissionActionResponse,
    AdminPermissionDetailResponse,
    AdminPermissionDisableRequest,
    AdminPermissionEndpointResponse,
    AdminPermissionListResponse,
    AdminPermissionResponse,
    AdminPermissionUpdate,
)


def permission_for_response() -> Permission:
    now = datetime(2026, 8, 14, 10, 30, tzinfo=UTC).replace(tzinfo=None)
    return Permission(
        id=7,
        name="app:read",
        display_name="  Read apps  ",
        description="List applications",
        is_declared=True,
        is_enabled=False,
        disabled_at=now,
        disabled_reason="maintenance",
        missing_at=None,
        created_at=now,
        updated_at=now,
        version=3,
    )


def response_for_permission() -> AdminPermissionResponse:
    permission = permission_for_response()
    return AdminPermissionResponse(
        id=permission.id,
        name=permission.name,
        display_name=permission.display_name,
        description=permission.description,
        resource="app",
        action="read",
        is_declared=permission.is_declared,
        is_enabled=permission.is_enabled,
        disabled_at=permission.disabled_at,
        disabled_reason=permission.disabled_reason,
        missing_at=permission.missing_at,
        endpoint_count=1,
        role_count=2,
        created_at=permission.created_at,
        updated_at=permission.updated_at,
        version=permission.version,
    )


def test_update_normalizes_text_and_allows_clearing_description() -> None:
    payload = AdminPermissionUpdate(
        display_name="  View applications  ",
        description="   ",
        version=3,
    )

    assert payload.display_name == "View applications"
    assert payload.description == ""
    assert payload.version == 3


def test_update_supports_partial_fields_but_requires_version() -> None:
    payload = AdminPermissionUpdate(description="  Details  ", version=4)

    assert payload.model_dump(exclude_unset=True) == {
        "description": "Details",
        "version": 4,
    }

    with pytest.raises(ValidationError):
        AdminPermissionUpdate(display_name=None, version=1)


def test_update_rejects_invalid_or_privileged_fields() -> None:
    invalid_payloads = (
        {},
        {"display_name": "   ", "version": 1},
        {"display_name": "x" * 129, "version": 1},
        {"description": "d" * 256, "version": 1},
        {"display_name": "name", "version": 0},
        {"display_name": "name", "version": True},
        {"display_name": "name", "version": 1, "name": "app:write"},
        {"display_name": "name", "version": 1, "is_enabled": True},
        {"display_name": "name", "version": 1, "endpoints": []},
    )

    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            AdminPermissionUpdate(**payload)


def test_disable_normalizes_optional_reason_and_rejects_extra_fields() -> None:
    assert AdminPermissionDisableRequest(reason="  emergency  ").reason == "emergency"
    assert AdminPermissionDisableRequest(reason="   ").reason is None
    assert AdminPermissionDisableRequest().reason is None

    with pytest.raises(ValidationError):
        AdminPermissionDisableRequest(reason="r" * 501)
    with pytest.raises(ValidationError):
        AdminPermissionDisableRequest(is_enabled=False)


def test_responses_expose_only_safe_permission_fields() -> None:
    response = response_for_permission()
    endpoint = AdminPermissionEndpointResponse(
        http_method="GET",
        path="/admin/apps",
        route_name="list_apps",
    )
    detail = AdminPermissionDetailResponse(**response.model_dump(), endpoints=[endpoint])
    action = AdminPermissionActionResponse(
        **response.model_dump(), changed=True, revoked_sessions=2
    )
    listing = AdminPermissionListResponse(items=[response], total=1, page=1, page_size=20)

    assert set(response.model_dump()) == {
        "id",
        "name",
        "display_name",
        "description",
        "resource",
        "action",
        "is_declared",
        "is_enabled",
        "disabled_at",
        "disabled_reason",
        "missing_at",
        "endpoint_count",
        "role_count",
        "created_at",
        "updated_at",
        "version",
    }
    assert detail.endpoints[0].path == "/admin/apps"
    assert action.revoked_sessions == 2
    assert listing.items[0].name == "app:read"

    with pytest.raises(ValidationError):
        AdminPermissionResponse(**response.model_dump(), token="secret")
    with pytest.raises(ValidationError):
        AdminPermissionActionResponse(
            **response.model_dump(), changed=True, revoked_sessions=-1
        )


def test_endpoint_response_rejects_internal_fields() -> None:
    with pytest.raises(ValidationError):
        AdminPermissionEndpointResponse(
            http_method="GET",
            path="/admin/apps",
            route_name="list_apps",
            permission_id=1,
        )

    endpoint = PermissionEndpoint(
        permission_id=1,
        http_method="GET",
        path="/admin/apps",
        route_name="list_apps",
    )
    assert AdminPermissionEndpointResponse.model_validate(endpoint, from_attributes=True).path == "/admin/apps"
