from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app

TEST_PRIVATE_KEY = """-----BEGIN PRIVATE KEY-----
MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC+Z6emQGbqLphz
OdHE4MhNdafo4XUxkRYob7UbDFW8/nT+pbLjjh5gxIefoRDxeqYhWgz1hBf3Vv27
AV2Ug6E8Vgts+iLkBUfd5J6LTVSxbu1xMHo66pqborFcyJcXDfJh2Kaz/HsXLwFG
m42wx4pNUvMSP72KIjexlpvbfG3GwVO610Nhz1RSyHcrOyzeDMOOU6FYKGynUk9B
g5tPItGLbOs2A6bCd03bLQddv60Quv1Cbqx9e1RabPa003Bks1uBT/OjD1LFD1CZ
4WIHyiPJbNc+PwG8raaVk6lie1AX+z5BGaz9O4LWgLR32GKLu2ezPw7nm5c6tIge
qFHgSoJHAgMBAAECggEBAJf6tte9+iecj7URhr2mSluBuUfqhhfNXilimOWBIAKd
/Raxfiuiad8Fn9erwZFuO6LNdSCXkmWr+xVEjsSXmKBHchFHS4hEKswTyvUYAa0r
BL3fWwEh98yYvQd5WRhe2oR9YPqzYjDsJRGN4jgj3eHAfyKm3AyhKWFH/RnhpOIK
VwhJqaGItTidthUJbFIYIuuZ5Vjohj6gGd4WooyQsQaiELCa1nzZzuMSmLJfUgp+
7QkOWW4BUZvESO/vEJcrcXpczkOsQqq2u78oOtsMxvXExFDlu8kDWvXquvkEajVj
a8SlHS/B13EsS5XqsWnwBawL0mp161IMAwro8cV7z6ECgYEA7RmmSDfD+2+6RBh5
L+KIQ+jymUDTGHJF0DGCRVOePmQDgzzj4n4+B47RpYu9LOxnBDehOgbW4ji+6oo/
leQf5I95Hj6PloDb2Pm7h2/RGhB7El5sv6hRjSRDjxdHAs9aFsFuFIG1oD20sxbG
EcQdpaa8qW2/K4D3oyD+tk8mHtcCgYEAzZUf6/QMnaw3PY6aV3ss2vc72APs02gG
otNW0BoCkCIEsKNVQquR2VLS/2kxsISiKJmuJhqHBrFGRpjp0MqyjoTkUBy9DZyF
xOO6AQr9J1PKbWOriPPxRwgyBqG100CvDAtLzhhFgtHwM4Ai1d5hToP4Pe7DPkRh
+hVkhfpwehECgYABZmxf8sxaeL9t1YMpsDnDxOVh2Esm0s3su84cILFHhwmqRbrG
xJ4TJ1m/k4KreD3nfXibQh0UuucNtYFInk8950b80bvBVMN3lYnw880VTVGcuygD
Pbg1kChB+Q43SwgqKDxBLL7o0lR11kWXJ0RRjRmCGp7NX/aWZQR8CR2dgwKBgFjS
NC+CiqzYyikbYo2nVzLnnIBw+bJBAJT60Egq5K6XNAWJG/4pGGOXyDe3oFNOiq0V
8MrfrTT0BJPd3y9pVAoFWotOT1QBKz5s0WE/+S4zooLujB8onjb9UHfTCDbUfIys
mLzbebTStX/avbI/WTVOCUPg05QkgVxGP98u28exAoGBAIu0+fCIYoy07yEmomhk
+HqsmDOq9mnC9H5y8xZ1z55SMqWoqc91e8HSsKmdQL0htcOHErjAGvlCRGP9vP1B
cifihzNMBiRtXx/XfG4UN5mmF5rJgdNvQPXuFhETyZ2E95f2ks3OZnYR0Ft1ejbZ
TZYO7jMxHqi6RD0PlREnq6CJ
-----END PRIVATE KEY-----"""
TEST_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAvmenpkBm6i6YcznRxODI
TXWn6OF1MZEWKG+1GwxVvP50/qWy444eYMSHn6EQ8XqmIVoM9YQX91b9uwFdlIOh
PFYLbPoi5AVH3eSei01UsW7tcTB6Ouqam6KxXMiXFw3yYdims/x7Fy8BRpuNsMeK
TVLzEj+9iiI3sZab23xtxsFTutdDYc9UUsh3Kzss3gzDjlOhWChsp1JPQYObTyLR
i2zrNgOmwndN2y0HXb+tELr9Qm6sfXtUWmz2tNNwZLNbgU/zow9SxQ9QmeFiB8oj
yWzXPj8BvK2mlZOpYntQF/s+QRms/TuC1oC0d9hii7tnsz8O55uXOrSIHqhR4EqC
RwIDAQAB
-----END PUBLIC KEY-----"""


@pytest.fixture(autouse=True)
def configure_test_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "jwt_private_key", TEST_PRIVATE_KEY)
    monkeypatch.setattr(settings, "jwt_public_key", TEST_PUBLIC_KEY)
    monkeypatch.setattr(settings, "jwt_issuer", "auth-service-test")
    monkeypatch.setattr(settings, "jwt_audience", "backend-api-test")
    yield


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def test_keys() -> dict[str, str]:
    return {"private_key": TEST_PRIVATE_KEY, "public_key": TEST_PUBLIC_KEY}
