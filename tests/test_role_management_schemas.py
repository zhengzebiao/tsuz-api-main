from datetime import datetime

import pytest
from pydantic import ValidationError

from app.models.permission import Permission
from app.models.role import Role
from app.schemas.admin_role import (
    AdminRoleActionResponse,
    AdminRoleCreate,
    AdminRoleDisableRequest,
    AdminRoleListResponse,
    AdminRolePermissionAssignment,
    AdminRolePermissionSummary,
    AdminRolePermissionsResponse,
    AdminRoleResponse,
    AdminRoleSummary,
    AdminRoleUpdate,
)
from app.schemas.admin_user import AdminUserRoleAssignment, AdminUserRolesResponse


def role_for_response() -> Role:
    now = datetime(2026, 8, 13, 10, 30)
    return Role(
        id=7,
        name="  auditor  ",
        description="Audit access",
        is_enabled=False,
        disabled_at=now,
        disabled_reason="  scheduled review  ",
        created_at=now,
        updated_at=now,
        version=3,
    )


def test_role_create_normalizes_text_and_uses_safe_defaults() -> None:
    payload = AdminRoleCreate(name="  auditor  ", description="  Audit access  ")

    assert payload.name == "auditor"
    assert payload.description == "Audit access"
    assert AdminRoleCreate(name="operator").description == ""


def test_role_create_rejects_blank_oversized_and_privileged_fields() -> None:
    invalid_payloads = (
        {"name": "   "},
        {"name": "a" * 65},
        {"name": "operator", "description": "d" * 256},
        {"name": "operator", "id": 1},
        {"name": "operator", "is_enabled": False},
        {"name": "operator", "version": 9},
        {"name": "operator", "disabled_at": None},
    )

    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            AdminRoleCreate(**payload)


def test_role_update_supports_partial_changes_and_clearing_description() -> None:
    name_update = AdminRoleUpdate(name="  renamed  ", version=2)
    description_update = AdminRoleUpdate(description="   ", version=2)

    assert name_update.name == "renamed"
    assert name_update.model_dump(exclude_unset=True) == {"name": "renamed", "version": 2}
    assert description_update.description == ""
    assert description_update.model_dump(exclude_unset=True) == {"description": "", "version": 2}


def test_role_update_requires_positive_version_and_rejects_null_or_status_fields() -> None:
    invalid_payloads = (
        {"name": "operator", "version": 0},
        {"name": "operator", "version": -1},
        {"name": "operator", "version": True},
        {"name": None, "version": 1},
        {"description": None, "version": 1},
        {"name": "   ", "version": 1},
        {"is_enabled": False, "version": 1},
        {"disabled_reason": "maintenance", "version": 1},
        {"created_at": datetime(2026, 8, 13), "version": 1},
    )

    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            AdminRoleUpdate(**payload)


def test_role_disable_normalizes_optional_reason_and_rejects_extra_fields() -> None:
    assert AdminRoleDisableRequest(reason="  maintenance  ").reason == "maintenance"
    assert AdminRoleDisableRequest(reason="   ").reason is None
    assert AdminRoleDisableRequest().reason is None

    with pytest.raises(ValidationError):
        AdminRoleDisableRequest(reason="r" * 501)
    with pytest.raises(ValidationError):
        AdminRoleDisableRequest(is_enabled=False)


def test_role_responses_expose_only_declared_safe_fields() -> None:
    role = role_for_response()
    response = AdminRoleResponse.model_validate(role)
    summary = AdminRoleSummary.model_validate(role)
    action = AdminRoleActionResponse(**response.model_dump(), changed=True, revoked_sessions=2)
    listing = AdminRoleListResponse(items=[response], total=1, page=1, page_size=20)

    assert set(response.model_dump()) == {
        "id",
        "name",
        "description",
        "is_enabled",
        "disabled_at",
        "disabled_reason",
        "created_at",
        "updated_at",
        "version",
    }
    assert set(summary.model_dump()) == {"id", "name", "description", "is_enabled"}
    assert action.changed is True
    assert action.revoked_sessions == 2
    assert listing.items[0].id == role.id

    with pytest.raises(ValidationError):
        AdminRoleResponse(**response.model_dump(), permissions=["role:read"])
    with pytest.raises(ValidationError):
        AdminRoleSummary(**summary.model_dump(), disabled_reason="forbidden")
    with pytest.raises(ValidationError):
        AdminRoleActionResponse(**response.model_dump(), changed=True, revoked_sessions=-1)


def test_role_permission_assignment_allows_empty_set_and_rejects_invalid_ids() -> None:
    empty = AdminRolePermissionAssignment(permission_ids=[], version=4)
    populated = AdminRolePermissionAssignment(permission_ids=[3, 8, 13], version=4)

    assert empty.permission_ids == []
    assert populated.permission_ids == [3, 8, 13]

    invalid_payloads = (
        {"permission_ids": [1, 1], "version": 1},
        {"permission_ids": [0], "version": 1},
        {"permission_ids": [-1], "version": 1},
        {"permission_ids": [True], "version": 1},
        {"permission_ids": [1], "version": 0},
        {"permission_ids": [1]},
        {"permission_ids": [1], "version": 1, "permissions": []},
    )

    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            AdminRolePermissionAssignment(**payload)


def test_role_permissions_response_contains_only_safe_permission_summaries() -> None:
    permission = Permission(
        id=9,
        name="app:read",
        display_name="View apps",
        description="Applications",
        is_declared=False,
        is_enabled=False,
    )
    summary = AdminRolePermissionSummary.model_validate(permission)
    response = AdminRolePermissionsResponse(
        role_id=7,
        permissions=[summary],
        version=5,
        changed=True,
        revoked_sessions=2,
    )

    assert response.permissions[0].id == permission.id
    assert set(response.model_dump()) == {
        "role_id",
        "permissions",
        "version",
        "changed",
        "revoked_sessions",
    }
    assert set(response.permissions[0].model_dump()) == {
        "id",
        "name",
        "display_name",
        "description",
        "is_declared",
        "is_enabled",
    }

    with pytest.raises(ValidationError):
        AdminRolePermissionSummary(
            **summary.model_dump(),
            endpoints=[],
        )
    with pytest.raises(ValidationError):
        AdminRolePermissionsResponse(
            role_id=7,
            permissions=[summary],
            version=5,
            changed=True,
            revoked_sessions=-1,
        )


def test_user_role_assignment_allows_empty_set_and_rejects_invalid_ids() -> None:
    empty = AdminUserRoleAssignment(role_ids=[], version=4)
    populated = AdminUserRoleAssignment(role_ids=[3, 8, 13], version=4)

    assert empty.role_ids == []
    assert populated.role_ids == [3, 8, 13]

    invalid_payloads = (
        {"role_ids": [1, 1], "version": 1},
        {"role_ids": [0], "version": 1},
        {"role_ids": [-1], "version": 1},
        {"role_ids": [True], "version": 1},
        {"role_ids": [1], "version": 0},
        {"role_ids": [1]},
        {"role_ids": [1], "version": 1, "roles": []},
    )

    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            AdminUserRoleAssignment(**payload)


def test_user_roles_response_contains_only_safe_role_summaries() -> None:
    role = role_for_response()
    summary = AdminRoleSummary.model_validate(role)
    response = AdminUserRolesResponse(
        user_id=11,
        roles=[summary],
        version=5,
        changed=True,
        revoked_sessions=1,
    )

    assert response.roles[0].id == role.id
    assert set(response.model_dump()) == {
        "user_id",
        "roles",
        "version",
        "changed",
        "revoked_sessions",
    }
    assert set(response.roles[0].model_dump()) == {"id", "name", "description", "is_enabled"}

    with pytest.raises(ValidationError):
        AdminUserRolesResponse(
            user_id=11,
            roles=[summary],
            version=5,
            changed=True,
            password_hash="forbidden",
        )
    with pytest.raises(ValidationError):
        AdminUserRolesResponse(
            user_id=11,
            roles=[{**summary.model_dump(), "permissions": ["role:read"]}],
            version=5,
            changed=True,
        )
