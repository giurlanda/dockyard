"""Beside the logo, signed-in people see the release Dockyard was built from."""

from collections.abc import Callable

from fastapi.testclient import TestClient

from dockyard.web import Instance, ListenerName

from .conftest import ADMINISTRATOR, AccessProxy, listener_client
from .test_local_sign_in import LOCAL_SIGN_IN, ON_THE_LAN


def test_signed_in_people_see_the_release_beside_the_logo(
    start_dockyard: Callable[..., TestClient], proxy: AccessProxy
) -> None:
    dockyard = start_dockyard(release="v0.2.0")

    page = dockyard.get("/", headers=proxy.headers(ADMINISTRATOR))

    assert '<span class="release" data-testid="release">v0.2.0</span>' in page.text


def test_outside_a_release_signed_in_people_see_dev(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    page = dockyard.get("/", headers=proxy.headers(ADMINISTRATOR))

    assert '<span class="release" data-testid="release">dev</span>' in page.text


def test_whoever_has_not_signed_in_sees_no_release(
    start_instance: Callable[..., Instance],
) -> None:
    instance = start_instance(release="v0.2.0", **LOCAL_SIGN_IN)
    local = listener_client(instance, ListenerName.LOCAL, address=ON_THE_LAN)

    page = local.get("/sign-in")

    assert page.status_code == 200
    assert "v0.2.0" not in page.text
    assert 'data-testid="release"' not in page.text
