from fastapi import APIRouter, Depends, FastAPI

from app.api.dependencies import require_permissions
from app.main import create_app
from app.services.permission_scanner import (
    AdminRoutePermissionCoverageError,
    PermissionScanError,
    ScannedPermissionBinding,
    scan_permission_routes,
)


def test_scanner_collects_permissions_from_route_and_nested_dependencies() -> None:
    app = FastAPI()

    def nested_permission(_actor=Depends(require_permissions("nested:read"))) -> None:
        return None

    @app.api_route(
        "/admin/items",
        methods=["GET", "POST"],
        dependencies=[
            Depends(require_permissions("item:read", "item:update")),
            Depends(require_permissions("item:read")),
            Depends(nested_permission),
        ],
        name="manage_items",
    )
    def manage_items() -> dict[str, bool]:
        return {"ok": True}

    result = scan_permission_routes(app)

    assert result.permission_names == ("item:read", "item:update", "nested:read")
    assert result.bindings == tuple(
        sorted(
            ScannedPermissionBinding(permission, method, "/admin/items", "manage_items")
            for permission in result.permission_names
            for method in ("GET", "POST")
        )
    )
    assert [route.http_method for route in result.routes] == ["GET", "POST"]
    assert all(
        route.required_permissions == ("item:read", "item:update", "nested:read") for route in result.routes
    )


def test_scanner_merges_router_and_include_level_dependencies() -> None:
    app = FastAPI()
    child = APIRouter(
        prefix="/items",
        dependencies=[Depends(require_permissions("router:read"))],
    )

    @child.get("/{item_id}", dependencies=[Depends(require_permissions("item:read"))])
    def get_item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    parent = APIRouter(prefix="/admin")
    parent.include_router(
        child,
        dependencies=[Depends(require_permissions("child:read"))],
    )
    app.include_router(
        parent,
        prefix="/v1",
        dependencies=[Depends(require_permissions("admin:read"))],
    )

    result = scan_permission_routes(app, admin_path_prefix="/v1/admin")

    assert result.permission_names == ("admin:read", "child:read", "item:read", "router:read")
    assert result.routes == (
        result.routes[0].__class__(
            http_method="GET",
            path="/v1/admin/items/{item_id}",
            route_name="get_item",
            required_permissions=("admin:read", "child:read", "item:read", "router:read"),
        ),
    )


def test_scanner_output_is_deterministic_and_excludes_unprotected_public_routes() -> None:
    app = FastAPI()

    @app.get("/health", name="health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/admin/z-last",
        dependencies=[Depends(require_permissions("z:last"))],
        name="z_last",
    )
    def z_last() -> None:
        return None

    @app.get(
        "/admin/a-first",
        dependencies=[Depends(require_permissions("a:first"))],
        name="a_first",
    )
    def a_first() -> None:
        return None

    first = scan_permission_routes(app)
    second = scan_permission_routes(app)

    assert first == second
    assert first.permission_names == ("a:first", "z:last")
    assert [binding.permission_name for binding in first.bindings] == ["a:first", "z:last"]
    assert not any(binding.path == "/health" for binding in first.bindings)
    assert any(route.path == "/health" and route.required_permissions == () for route in first.routes)


def test_scanner_rejects_unprotected_admin_route_unless_explicitly_allowed() -> None:
    app = FastAPI()

    @app.get("/admin/status", name="admin_status")
    def admin_status() -> dict[str, str]:
        return {"status": "ok"}

    try:
        scan_permission_routes(app)
    except AdminRoutePermissionCoverageError as exc:
        assert "GET /admin/status" in str(exc)
    else:
        raise AssertionError("unprotected admin route should fail coverage validation")

    result = scan_permission_routes(app, admin_route_allowlist={("get", "/admin/status")})
    assert result.permission_names == ()
    assert result.bindings == ()


def test_scanner_rejects_conflicting_route_names_for_the_same_binding() -> None:
    app = FastAPI()

    @app.get(
        "/admin/items",
        dependencies=[Depends(require_permissions("item:read"))],
        name="first_items",
    )
    def first_items() -> None:
        return None

    @app.get(
        "/admin/items",
        dependencies=[Depends(require_permissions("item:read"))],
        name="second_items",
    )
    def second_items() -> None:
        return None

    try:
        scan_permission_routes(app)
    except PermissionScanError as exc:
        assert "conflicting route names" in str(exc)
    else:
        raise AssertionError("conflicting bindings should fail scanning")


def test_main_application_permission_catalog_and_admin_coverage() -> None:
    result = scan_permission_routes(create_app())

    assert result.permission_names == (
        "app:create",
        "app:disable",
        "app:enable",
        "app:read",
        "app:regenerate_secret",
        "app:update",
        "permission:disable",
        "permission:enable",
        "permission:read",
        "permission:update",
        "role:assign_permissions",
        "role:create",
        "role:disable",
        "role:enable",
        "role:read",
        "role:update",
        "user:assign_roles",
        "user:blacklist",
        "user:create",
        "user:disable",
        "user:enable",
        "user:force_logout",
        "user:read",
        "user:recover",
        "user:reset_password",
        "user:update",
    )
    assert len(result.bindings) == 33
    assert len([route for route in result.routes if route.path.startswith("/admin/") or route.path == "/admin"]) == 33
    assert not any(binding.path in {"/health", "/auth/login", "/auth/refresh"} for binding in result.bindings)
    assert all(route.required_permissions for route in result.routes if route.path.startswith("/admin/"))
