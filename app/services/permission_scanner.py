from collections.abc import Collection, Iterator
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

from app.api.dependencies import PermissionDeclarationError, validate_permission_names


class PermissionScanError(RuntimeError):
    pass


class AdminRoutePermissionCoverageError(PermissionScanError):
    pass


@dataclass(frozen=True, order=True)
class ScannedPermissionBinding:
    permission_name: str
    http_method: str
    path: str
    route_name: str


@dataclass(frozen=True, order=True)
class ScannedRoute:
    http_method: str
    path: str
    route_name: str
    required_permissions: tuple[str, ...]


@dataclass(frozen=True)
class PermissionScanResult:
    permission_names: tuple[str, ...]
    bindings: tuple[ScannedPermissionBinding, ...]
    routes: tuple[ScannedRoute, ...]


def scan_permission_routes(
    application: FastAPI | APIRouter,
    *,
    admin_path_prefix: str = "/admin",
    admin_route_allowlist: Collection[tuple[str, str]] = (),
) -> PermissionScanResult:
    allowlist = _normalize_allowlist(admin_route_allowlist)
    scanned_routes: list[ScannedRoute] = []
    bindings: set[ScannedPermissionBinding] = set()
    binding_route_names: dict[tuple[str, str, str], str] = {}

    for route in _iter_effective_api_routes(application):
        path = route.path
        route_name = route.name
        if not isinstance(path, str) or not path.startswith("/"):
            raise PermissionScanError(f"route has an invalid path: {path!r}")
        if not isinstance(route_name, str) or not route_name:
            raise PermissionScanError(f"route {path!r} has an invalid name")

        permissions = _read_required_permissions(route.dependant)
        for method in sorted(route.methods or ()):
            http_method = method.upper()
            scanned_route = ScannedRoute(
                http_method=http_method,
                path=path,
                route_name=route_name,
                required_permissions=permissions,
            )
            scanned_routes.append(scanned_route)

            is_admin_route = path == admin_path_prefix or path.startswith(f"{admin_path_prefix.rstrip('/')}/")
            if is_admin_route and not permissions and (http_method, path) not in allowlist:
                raise AdminRoutePermissionCoverageError(
                    f"admin route {http_method} {path} ({route_name}) has no permission declaration"
                )

            for permission_name in permissions:
                binding_key = (permission_name, http_method, path)
                previous_route_name = binding_route_names.setdefault(binding_key, route_name)
                if previous_route_name != route_name:
                    raise PermissionScanError(
                        "conflicting route names for permission binding "
                        f"{permission_name} {http_method} {path}: {previous_route_name!r} and {route_name!r}"
                    )
                bindings.add(
                    ScannedPermissionBinding(
                        permission_name=permission_name,
                        http_method=http_method,
                        path=path,
                        route_name=route_name,
                    )
                )

    ordered_bindings = tuple(sorted(bindings))
    return PermissionScanResult(
        permission_names=tuple(sorted({binding.permission_name for binding in ordered_bindings})),
        bindings=ordered_bindings,
        routes=tuple(sorted(set(scanned_routes))),
    )


def _iter_effective_api_routes(application: FastAPI | APIRouter) -> Iterator[Any]:
    router = application.router if isinstance(application, FastAPI) else application
    for registered_route in router.routes:
        if isinstance(registered_route, APIRoute):
            yield registered_route
            continue

        effective_route_contexts = getattr(registered_route, "effective_route_contexts", None)
        if effective_route_contexts is None:
            continue
        for route_context in effective_route_contexts():
            if isinstance(route_context.original_route, APIRoute):
                yield route_context


def _read_required_permissions(dependant: Any) -> tuple[str, ...]:
    permissions: list[str] = []
    visited: set[int] = set()
    stack = [dependant]

    while stack:
        current = stack.pop()
        dependant_id = id(current)
        if dependant_id in visited:
            continue
        visited.add(dependant_id)

        call = getattr(current, "call", None)
        declared_permissions = getattr(call, "required_permissions", None)
        if declared_permissions is not None:
            if not isinstance(declared_permissions, tuple):
                raise PermissionScanError("permission dependency metadata must be an immutable tuple")
            try:
                permissions.extend(validate_permission_names(declared_permissions))
            except PermissionDeclarationError as exc:
                raise PermissionScanError(f"invalid permission dependency metadata: {exc}") from exc

        stack.extend(reversed(getattr(current, "dependencies", ())))

    return tuple(sorted(set(permissions)))


def _normalize_allowlist(allowlist: Collection[tuple[str, str]]) -> frozenset[tuple[str, str]]:
    normalized: set[tuple[str, str]] = set()
    for entry in allowlist:
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise PermissionScanError("admin route allowlist entries must be (method, path) tuples")
        method, path = entry
        if not isinstance(method, str) or not method or method != method.strip():
            raise PermissionScanError(f"invalid allowlist method: {method!r}")
        if not isinstance(path, str) or not path.startswith("/") or path != path.strip():
            raise PermissionScanError(f"invalid allowlist path: {path!r}")
        normalized.add((method.upper(), path))
    return frozenset(normalized)
