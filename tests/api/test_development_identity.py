"""The development identity: a plain header, accepted only when explicitly enabled."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from .conftest import ADMINISTRATOR

DEV_HEADER = "X-Dockyard-Dev-Email"


def test_the_development_header_is_ignored_by_default(dockyard: TestClient) -> None:
    response = dockyard.get("/api/me", headers={DEV_HEADER: ADMINISTRATOR})

    assert response.status_code == 401


@pytest.mark.parametrize("value", ["false", "0", "no", ""])
def test_the_development_header_is_ignored_when_switched_off(
    start_dockyard: Callable[..., TestClient], value: str
) -> None:
    dockyard = start_dockyard(DOCKYARD_DEV_IDENTITY=value)

    assert dockyard.get("/api/me", headers={DEV_HEADER: ADMINISTRATOR}).status_code == 401


@pytest.mark.parametrize("value", ["true", "1", "yes", "TRUE"])
def test_the_development_header_identifies_people_when_enabled(
    start_dockyard: Callable[..., TestClient], value: str
) -> None:
    dockyard = start_dockyard(DOCKYARD_DEV_IDENTITY=value)

    me = dockyard.get("/api/me", headers={DEV_HEADER: ADMINISTRATOR})

    assert me.json() == {"email": ADMINISTRATOR, "role": "administrator"}


def test_the_development_identity_needs_no_proxy_settings(
    start_dockyard: Callable[..., TestClient],
) -> None:
    dockyard = start_dockyard(
        DOCKYARD_DEV_IDENTITY="true", DOCKYARD_CF_ACCESS_TEAM=None, DOCKYARD_CF_ACCESS_AUD=None
    )

    assert dockyard.get("/api/me", headers={DEV_HEADER: ADMINISTRATOR}).status_code == 200


def test_the_development_identity_still_refuses_people_who_are_not_users(
    start_dockyard: Callable[..., TestClient],
) -> None:
    dockyard = start_dockyard(DOCKYARD_DEV_IDENTITY="true")

    assert dockyard.get("/", headers={DEV_HEADER: "stranger@example.com"}).status_code == 403
    assert dockyard.get("/").status_code == 401
