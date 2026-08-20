import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api import dependencies
from app.api.dependencies import PERMISSION_NAME_MAX_LENGTH, PermissionDeclarationError
from app.models.user import User
from app.services.authorization_service import AuthenticationError, PermissionDeniedError


def build_client(monkeypatch: pytest.MonkeyPatch, service_class: type) -> TestClient:
    app = FastAPI()
    monkeypatch.setattr(dependencies, "AuthorizationService", service_class)

    @app.get("/protected")
    def protected(user: User = Depends(dependencies.require_permissions("user:read"))) -> dict[str, int]:
        return {"user_id": user.id}

    return TestClient(app)


def test_permission_dependency_exposes_immutable_metadata() -> None:
    dependency = dependencies.require_permissions("app:read", "app:update")

    assert dependency.required_permissions == ("app:read", "app:update")
    assert isinstance(dependency.required_permissions, tuple)


def test_permission_dependency_requires_at_least_one_permission() -> None:
    with pytest.raises(PermissionDeclarationError, match="at least one permission"):
        dependencies.require_permissions()


@pytest.mark.parametrize(
    "permissions",
    [
        (None,),
        (1,),
        ("",),
        (" app:read",),
        ("app:read ",),
        ("App:Read",),
        ("app read",),
        ("app:read:detail",),
        ("app-read",),
        ("1app:read",),
        ("app:1read",),
        (f"a:{'b' * (PERMISSION_NAME_MAX_LENGTH - 1)}",),
        ("app:read", "app:read"),
    ],
)
def test_permission_dependency_rejects_invalid_declarations(permissions: tuple[object, ...]) -> None:
    with pytest.raises(PermissionDeclarationError):
        dependencies.require_permissions(*permissions)


def test_permission_dependency_returns_authenticated_user(monkeypatch: pytest.MonkeyPatch) -> None:
    class AllowedAuthorizationService:
        def __init__(self, db) -> None:
            self.db = db

        def require_permissions(self, access_token: str, required_permissions: tuple[str, ...]) -> User:
            assert access_token == "access"
            assert required_permissions == ("user:read",)
            return User(id=7, email="admin@example.com", hashed_password="hashed-password")

    with build_client(monkeypatch, AllowedAuthorizationService) as client:
        response = client.get("/protected", headers={"Authorization": "Bearer access"})

    assert response.status_code == 200
    assert response.json() == {"user_id": 7}


def test_permission_dependency_returns_401_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    class UnusedAuthorizationService:
        def __init__(self, db) -> None:
            raise AssertionError("authorization service should not be created")

    with build_client(monkeypatch, UnusedAuthorizationService) as client:
        response = client.get("/protected")

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid access token"


def test_permission_dependency_maps_authentication_failure_to_401(monkeypatch: pytest.MonkeyPatch) -> None:
    class RejectedAuthorizationService:
        def __init__(self, db) -> None:
            self.db = db

        def require_permissions(self, access_token: str, required_permissions: tuple[str, ...]) -> User:
            raise AuthenticationError("invalid access token")

    with build_client(monkeypatch, RejectedAuthorizationService) as client:
        response = client.get("/protected", headers={"Authorization": "Bearer access"})

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid access token"


def test_permission_dependency_maps_missing_scope_to_403(monkeypatch: pytest.MonkeyPatch) -> None:
    class ForbiddenAuthorizationService:
        def __init__(self, db) -> None:
            self.db = db

        def require_permissions(self, access_token: str, required_permissions: tuple[str, ...]) -> User:
            raise PermissionDeniedError("insufficient permissions")

    with build_client(monkeypatch, ForbiddenAuthorizationService) as client:
        response = client.get("/protected", headers={"Authorization": "Bearer access"})

    assert response.status_code == 403
    assert response.json()["detail"] == "insufficient permissions"
