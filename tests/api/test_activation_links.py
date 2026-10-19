"""Activation links: while Local sign-in is on, an Administrator who invites a person gets a link
with which the person, on the local listener, chooses a password and becomes a User."""

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from dockyard.web import Instance, ListenerName

from .conftest import ADMINISTRATOR, AccessProxy, FakeClock, listener_client

LOCAL_SIGN_IN = {"DOCKYARD_LOCAL_PORT": "8443", "DOCKYARD_LOCAL_ADDRESS": "192.168.1.10"}
ON_THE_LAN = "192.168.1.20"
INVITED = "invited.person@example.com"
PASSWORD = "correct horse battery staple"


@pytest.fixture
def instance(start_instance: Callable[..., Instance]) -> Instance:
    return start_instance(**LOCAL_SIGN_IN)


@pytest.fixture
def tunnel(instance: Instance) -> TestClient:
    return listener_client(instance, ListenerName.TUNNEL)


@pytest.fixture
def local(instance: Instance) -> TestClient:
    return listener_client(instance, ListenerName.LOCAL, address=ON_THE_LAN)


def invite(tunnel: TestClient, proxy: AccessProxy, email: str = INVITED) -> Any:
    return tunnel.post(
        "/api/invitations", json={"email": email}, headers=proxy.headers(ADMINISTRATOR)
    )


def test_an_invitation_made_while_local_sign_in_is_on_comes_with_its_activation_link(
    tunnel: TestClient, proxy: AccessProxy
) -> None:
    invited = invite(tunnel, proxy)

    assert invited.status_code == 201
    link = urlsplit(invited.json()["activation_link"])
    assert (link.scheme, link.netloc) == ("https", "192.168.1.10:8443")
    assert link.path.startswith("/activate/")


def test_without_local_sign_in_an_invitation_has_no_activation_link(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    invited = invite(dockyard, proxy)

    assert invited.status_code == 201
    assert invited.json()["activation_link"] is None


@pytest.mark.parametrize(
    ("settings", "base"),
    [
        ({"DOCKYARD_LOCAL_URL": "https://dockyard.lan:9443"}, "https://dockyard.lan:9443"),
        ({"DOCKYARD_LOCAL_URL": "https://dockyard.lan/"}, "https://dockyard.lan"),
        ({"DOCKYARD_LOCAL_HOST_PORT": "443"}, "https://192.168.1.10:443"),
    ],
    ids=["local-url", "local-url-with-slash", "published-port"],
)
def test_the_activation_link_points_where_people_open_local_sign_in(
    start_instance: Callable[..., Instance],
    proxy: AccessProxy,
    settings: dict[str, str],
    base: str,
) -> None:
    tunnel = listener_client(start_instance(**LOCAL_SIGN_IN, **settings), ListenerName.TUNNEL)

    link = invite(tunnel, proxy).json()["activation_link"]

    assert link.startswith(f"{base}/activate/")


def path_of(link: str) -> str:
    return urlsplit(link).path


def activation_link(tunnel: TestClient, proxy: AccessProxy, email: str = INVITED) -> str:
    return path_of(invite(tunnel, proxy, email).json()["activation_link"])


def activate(local: TestClient, link: str, password: str = PASSWORD, **form: str) -> Any:
    return local.post(link, data={"password": password, "repeated": password, **form})


def test_the_activation_link_asks_for_a_password_and_explains_the_rules(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy
) -> None:
    page = local.get(activation_link(tunnel, proxy))

    assert page.status_code == 200
    assert INVITED in page.text
    assert "At least 12 characters, and not your email." in page.text
    assert 'name="password"' in page.text


def test_choosing_a_password_makes_the_person_a_user_signed_in_on_their_dashboard(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy
) -> None:
    link = activation_link(tunnel, proxy)

    activated = activate(local, link)

    assert activated.status_code == 200
    assert activated.url.path == "/"
    assert INVITED in activated.text
    assert local.get("/api/me").json() == {"email": INVITED, "role": "user"}
    administrator = proxy.headers(ADMINISTRATOR)
    assert tunnel.get("/api/invitations", headers=administrator).json() == []
    assert INVITED in [
        user["email"] for user in tunnel.get("/api/users", headers=administrator).json()
    ]


def test_the_password_chosen_with_the_link_signs_in_afterwards(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy
) -> None:
    activate(local, activation_link(tunnel, proxy))
    local.post("/sign-out")

    local.post("/sign-in", data={"email": INVITED, "password": PASSWORD})

    assert local.get("/api/me").json()["email"] == INVITED


@pytest.mark.parametrize(
    ("password", "repeated", "reason"),
    [
        (
            "eleven char",
            "eleven char",
            "The password is too short: it needs at least 12 characters.",
        ),
        (INVITED.upper(), INVITED.upper(), "The password cannot be your email."),
        (PASSWORD, PASSWORD + "!", "The two passwords are different."),
    ],
    ids=["too-short", "the-email", "repeated-differently"],
)
def test_a_password_that_breaks_the_rules_is_refused_and_the_link_still_works(
    tunnel: TestClient,
    local: TestClient,
    proxy: AccessProxy,
    password: str,
    repeated: str,
    reason: str,
) -> None:
    link = activation_link(tunnel, proxy)

    refused = activate(local, link, password, repeated=repeated)

    assert refused.status_code == 422
    assert reason in refused.text
    assert local.get("/api/me").status_code == 401
    assert activate(local, link).url.path == "/"


def assert_the_link_no_longer_works(local: TestClient, link: str) -> None:
    for response in (local.get(link), activate(local, link)):
        assert response.status_code == 410
        assert "Ask an Administrator for a new one." in response.text
        assert 'name="password"' not in response.text


def test_an_activation_link_works_once(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy
) -> None:
    link = activation_link(tunnel, proxy)
    activate(local, link)
    local.post("/sign-out")

    assert_the_link_no_longer_works(local, link)
    assert local.get("/api/me").status_code == 401


def test_an_activation_link_expires_after_seven_days(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy, clock: FakeClock
) -> None:
    link = activation_link(tunnel, proxy)

    clock.advance(days=7, seconds=-1)
    assert local.get(link).status_code == 200
    clock.advance(seconds=1)

    assert_the_link_no_longer_works(local, link)


def test_an_unknown_activation_link_says_the_same_in_italian(local: TestClient) -> None:
    local.headers["Accept-Language"] = "it"

    unknown = local.get("/activate/not-a-token")

    assert unknown.status_code == 410
    assert "Chiedine uno nuovo a un Amministratore." in unknown.text


def new_link(
    tunnel: TestClient, proxy: AccessProxy, email: str = INVITED, by: str = ADMINISTRATOR
) -> Any:
    return tunnel.post(f"/api/invitations/{email}/activation-link", headers=proxy.headers(by))


def test_a_new_activation_link_voids_the_previous_one(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy, clock: FakeClock
) -> None:
    previous = activation_link(tunnel, proxy)
    clock.advance(days=6)

    made = new_link(tunnel, proxy)

    assert made.status_code == 201
    link = path_of(made.json()["activation_link"])
    assert link != previous
    assert_the_link_no_longer_works(local, previous)
    clock.advance(days=6)  # seven days from the new one, not from the Invitation
    assert activate(local, link).url.path == "/"


def test_a_new_activation_link_is_only_for_a_pending_invitation(
    tunnel: TestClient, proxy: AccessProxy
) -> None:
    assert new_link(tunnel, proxy, "nobody@example.com").status_code == 404
    assert new_link(tunnel, proxy, ADMINISTRATOR).status_code == 404


def test_without_local_sign_in_there_are_no_activation_links_to_make(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    invite(dockyard, proxy)

    assert new_link(dockyard, proxy).status_code == 404


def test_revoking_the_invitation_voids_its_activation_link(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy
) -> None:
    link = activation_link(tunnel, proxy)

    tunnel.delete(f"/api/invitations/{INVITED}", headers=proxy.headers(ADMINISTRATOR))

    assert_the_link_no_longer_works(local, link)
    invite(tunnel, proxy)  # invited again: still not with the old link
    assert_the_link_no_longer_works(local, link)


def test_arriving_through_the_authentication_proxy_first_uses_up_the_invitation_and_its_link(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy
) -> None:
    link = activation_link(tunnel, proxy)

    assert tunnel.get("/api/me", headers=proxy.headers(INVITED)).status_code == 200

    assert_the_link_no_longer_works(local, link)


def test_activating_first_then_arriving_through_the_authentication_proxy_is_the_same_user(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy
) -> None:
    activate(local, activation_link(tunnel, proxy))

    me = tunnel.get("/api/me", headers=proxy.headers(INVITED))

    assert me.json() == {"email": INVITED, "role": "user"}
    users = tunnel.get("/api/users", headers=proxy.headers(ADMINISTRATOR)).json()
    assert [user["email"] for user in users].count(INVITED) == 1


def test_the_tunnel_listener_has_no_activation_links(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy
) -> None:
    link = activation_link(tunnel, proxy)

    administrator = proxy.headers(ADMINISTRATOR)
    form = {"password": PASSWORD, "repeated": PASSWORD}

    assert tunnel.get(link, headers=administrator).status_code == 404
    assert tunnel.post(link, data=form, headers=administrator).status_code == 404
    assert tunnel.post(link, data=form).status_code == 401
    assert local.get(link).status_code == 200


def test_only_administrators_make_activation_links(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy
) -> None:
    invite(tunnel, proxy)
    invite(tunnel, proxy, "user@example.com")
    tunnel.get("/api/me", headers=proxy.headers("user@example.com"))

    refused = new_link(tunnel, proxy, by="user@example.com")

    assert refused.status_code == 403
    assert refused.json()["error"] == "administrator_only"
    assert "/activate/" not in tunnel.get("/users", headers=proxy.headers("user@example.com")).text


def link_on_page(page: str) -> str:
    match = re.search(r'value="(https://[^"]+/activate/[^"]+)"', page)
    assert match, "no Activation link on the page"
    return match.group(1)


@pytest.mark.parametrize("listener", [ListenerName.TUNNEL, ListenerName.LOCAL])
def test_the_users_page_shows_the_new_invitations_activation_link_to_copy(
    instance: Instance, local: TestClient, proxy: AccessProxy, listener: ListenerName
) -> None:
    if listener == ListenerName.TUNNEL:
        page = listener_client(instance, listener)
        page.headers.update(proxy.headers(ADMINISTRATOR))
    else:
        instance.local_sign_in.set_password(ADMINISTRATOR, PASSWORD)
        page = listener_client(instance, listener, address=ON_THE_LAN)
        page.post("/sign-in", data={"email": ADMINISTRATOR, "password": PASSWORD})

    invited = page.post("/invitations", data={"email": INVITED})

    assert invited.status_code == 201
    assert "Copy" in invited.text
    link = link_on_page(invited.text)
    assert link.startswith("https://192.168.1.10:8443/activate/")
    local.cookies.clear()
    assert local.get(path_of(link)).status_code == 200
    assert "/activate/" not in page.get("/users").text  # shown once, as it is made


def test_the_users_page_makes_a_new_activation_link_voiding_the_previous_one(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy
) -> None:
    tunnel.headers.update(proxy.headers(ADMINISTRATOR))
    previous = link_on_page(tunnel.post("/invitations", data={"email": INVITED}).text)
    assert f"New Activation link for {INVITED}" in tunnel.get("/users").text

    made = tunnel.post(f"/invitations/{INVITED}/activation-link")

    assert made.status_code == 201
    link = link_on_page(made.text)
    assert link != previous
    assert_the_link_no_longer_works(local, path_of(previous))
    assert local.get(path_of(link)).status_code == 200
    assert tunnel.post("/invitations/nobody@example.com/activation-link").status_code == 404


def test_without_local_sign_in_the_users_page_has_no_activation_links(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    dockyard.headers.update(proxy.headers(ADMINISTRATOR))

    invited = dockyard.post("/invitations", data={"email": INVITED})

    assert invited.url.path == "/users"
    assert INVITED in invited.text
    assert "/activate/" not in invited.text
    assert "New Activation link" not in invited.text
    assert dockyard.post(f"/invitations/{INVITED}/activation-link").status_code == 404


def test_the_activation_link_on_the_users_page_speaks_italian(
    tunnel: TestClient, proxy: AccessProxy
) -> None:
    tunnel.headers.update({**proxy.headers(ADMINISTRATOR), "Accept-Language": "it"})

    invited = tunnel.post("/invitations", data={"email": INVITED})

    assert "Copia" in invited.text
    assert f"Nuovo Link di attivazione per {INVITED}" in invited.text
