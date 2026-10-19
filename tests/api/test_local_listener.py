"""The local listener: Local sign-in's own way in, only from the Local networks (ADR 0004).

Nobody can sign in on it yet: every request that gets past the address check is not signed in.
"""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from dockyard.runtime.fake import FakeRuntime
from dockyard.web import Instance, ListenerName

from .conftest import ADMINISTRATOR, CLOUDFLARED, AccessProxy, listener_client

LOCAL_SIGN_IN = {"DOCKYARD_LOCAL_PORT": "8443", "DOCKYARD_LOCAL_ADDRESS": "192.168.1.10"}
ON_THE_LAN = "192.168.1.20"
ON_THE_VPN = "100.101.102.103"  # Tailscale
ON_THE_INTERNET = "203.0.113.5"
ITALIAN = {"Accept-Language": "it-IT,it;q=0.9"}


@pytest.fixture
def start_local(start_instance: Callable[..., Instance]) -> Callable[..., TestClient]:
    """A client of the local listener, reaching it from `address`."""

    def start(address: str, **overrides: str | None) -> TestClient:
        instance = start_instance(**LOCAL_SIGN_IN, **overrides)
        return listener_client(instance, ListenerName.LOCAL, address=address)

    return start


def test_local_sign_in_adds_a_local_listener_beside_the_tunnel_one(
    start_instance: Callable[..., Instance],
) -> None:
    instance = start_instance(**LOCAL_SIGN_IN, DOCKYARD_LISTEN_HOST="0.0.0.0")

    assert {name: listener.port for name, listener in instance.listeners.items()} == {
        ListenerName.TUNNEL: 8000,
        ListenerName.LOCAL: 8443,
    }
    assert instance.listeners[ListenerName.LOCAL].host == "0.0.0.0"


def test_without_cloudflare_there_is_only_the_local_listener(
    start_instance: Callable[..., Instance],
) -> None:
    instance = start_instance(
        **LOCAL_SIGN_IN, DOCKYARD_CF_ACCESS_TEAM=None, DOCKYARD_CF_ACCESS_AUD=None
    )

    assert list(instance.listeners) == [ListenerName.LOCAL]


@pytest.mark.parametrize("address", [ON_THE_LAN, ON_THE_VPN])
@pytest.mark.parametrize("path", ["/", "/api/me", "/no/such/page"])
def test_from_the_local_networks_nobody_is_signed_in_yet(
    start_local: Callable[..., TestClient], address: str, path: str
) -> None:
    assert start_local(address).get(path).status_code == 401


@pytest.mark.parametrize("path", ["/", "/no/such/page", "/static/terminal.js"])
def test_from_outside_the_local_networks_a_page_is_refused_revealing_nothing(
    start_local: Callable[..., TestClient], path: str
) -> None:
    page = start_local(ON_THE_INTERNET).get(path)

    assert page.status_code == 403
    assert "not reachable from your network" in page.text
    for revealing in ("Dockyard", ADMINISTRATOR, "Projects", "Sign", "<script"):
        assert revealing not in page.text


def test_from_outside_the_local_networks_the_api_is_refused(
    start_local: Callable[..., TestClient],
) -> None:
    response = start_local(ON_THE_INTERNET).get("/api/me")

    assert response.status_code == 403
    assert response.json()["error"] == "outside_local_networks"


def test_from_outside_the_local_networks_websockets_are_refused(
    start_local: Callable[..., TestClient],
) -> None:
    local = start_local(ON_THE_INTERNET)

    with pytest.raises(WebSocketDisconnect) as refused, local.websocket_connect("/ws/x") as ws:
        ws.receive_text()

    assert refused.value.code == 4403


def test_the_refusal_speaks_the_language_of_whoever_asks(
    start_local: Callable[..., TestClient],
) -> None:
    page = start_local(ON_THE_INTERNET).get("/", headers=ITALIAN)

    assert page.status_code == 403
    assert "non è raggiungibile dalla tua rete" in page.text


def test_the_local_networks_can_be_narrowed(start_local: Callable[..., TestClient]) -> None:
    networks = {"DOCKYARD_LOCAL_NETWORKS": "192.168.1.0/24, 100.64.0.0/10"}

    assert start_local("192.168.1.99", **networks).get("/api/me").status_code == 401
    assert start_local(ON_THE_VPN, **networks).get("/api/me").status_code == 401
    assert start_local("192.168.2.99", **networks).get("/api/me").status_code == 403
    assert start_local("10.0.0.5", **networks).get("/api/me").status_code == 403


def test_from_where_cloudflared_reaches_dockyard_the_local_listener_refuses(
    start_local: Callable[..., TestClient], proxy: AccessProxy
) -> None:
    """The tunnel's traffic comes from a private Docker address: it must never pass as LAN."""
    response = start_local(CLOUDFLARED).get("/api/me", headers=proxy.headers(ADMINISTRATOR))

    assert response.status_code == 403
    assert response.json()["error"] == "outside_local_networks"


def test_every_network_docker_uses_is_refused_even_inside_the_local_networks(
    start_local: Callable[..., TestClient], runtime: FakeRuntime
) -> None:
    """Projects, the Docker bridge gateway (docker-proxy) and Dockyard's own networks."""
    networks = runtime.networks()
    assert networks

    for network in networks:
        for address in (network[1], network[-2]):  # the gateway, the last container
            response = start_local(str(address)).get("/api/me")
            assert response.json()["error"] == "outside_local_networks", address


def test_a_cloudflare_access_token_is_ignored_on_the_local_listener(
    start_local: Callable[..., TestClient], proxy: AccessProxy
) -> None:
    local = start_local(ON_THE_LAN)

    by_header = local.get("/api/me", headers=proxy.headers(ADMINISTRATOR))
    local.cookies.set("CF_Authorization", proxy.token(ADMINISTRATOR))
    by_cookie = local.get("/api/me")

    for response in (by_header, by_cookie):
        assert response.status_code == 401
        assert response.json()["error"] == "unauthenticated"


def test_from_the_local_networks_websockets_are_not_signed_in(
    start_local: Callable[..., TestClient], proxy: AccessProxy
) -> None:
    local = start_local(ON_THE_LAN)

    with (
        pytest.raises(WebSocketDisconnect) as refused,
        local.websocket_connect("/ws/x", headers=proxy.headers(ADMINISTRATOR)) as ws,
    ):
        ws.receive_text()

    assert refused.value.code == 4401
