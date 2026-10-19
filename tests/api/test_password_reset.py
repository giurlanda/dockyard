"""Password reset by Activation link: a User forgot their password, an Administrator makes them
an Activation link, and with it the User chooses a new one on the local listener, signing out
everywhere else."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dockyard.web import Instance, ListenerName

from .conftest import ADMINISTRATOR, AccessProxy, FakeClock, listener_client
from .test_activation_links import (
    activate,
    assert_the_link_no_longer_works,
    link_on_page,
    path_of,
)
from .test_local_sign_in import LOCAL_SIGN_IN, ON_THE_LAN, PASSWORD, sign_in

USER = "forgetful.user@example.com"
NEW_PASSWORD = "a password I will remember"


@pytest.fixture
def instance(start_instance: Callable[..., Instance]) -> Instance:
    return start_instance(**LOCAL_SIGN_IN)


@pytest.fixture
def tunnel(instance: Instance) -> TestClient:
    return listener_client(instance, ListenerName.TUNNEL)


@pytest.fixture
def local(instance: Instance) -> TestClient:
    return listener_client(instance, ListenerName.LOCAL, address=ON_THE_LAN)


@pytest.fixture
def user(instance: Instance, tunnel: TestClient, proxy: AccessProxy) -> str:
    """A User with a password, who forgot it."""
    tunnel.post("/api/invitations", json={"email": USER}, headers=proxy.headers(ADMINISTRATOR))
    assert tunnel.get("/api/me", headers=proxy.headers(USER)).status_code == 200
    instance.local_sign_in.set_password(USER, PASSWORD)
    return USER


def new_link_to_reset(
    tunnel: TestClient, proxy: AccessProxy, email: str = USER, by: str = ADMINISTRATOR
) -> Any:
    return tunnel.post(f"/api/users/{email}/activation-link", headers=proxy.headers(by))


def a_link_to_reset(tunnel: TestClient, proxy: AccessProxy) -> str:
    return path_of(new_link_to_reset(tunnel, proxy).json()["activation_link"])


def test_the_new_password_chosen_with_the_link_signs_the_user_in(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy, user: str
) -> None:
    made = new_link_to_reset(tunnel, proxy)
    assert made.status_code == 201
    link = path_of(made.json()["activation_link"])

    reset = activate(local, link, NEW_PASSWORD)

    assert reset.status_code == 200
    assert reset.url.path == "/"
    assert local.get("/api/me").json() == {"email": USER, "role": "user"}
    local.post("/sign-out")
    assert sign_in(local, USER, PASSWORD).status_code == 401
    assert sign_in(local, USER, NEW_PASSWORD).url.path == "/"


def test_resetting_signs_the_user_out_everywhere_else(
    instance: Instance, tunnel: TestClient, local: TestClient, proxy: AccessProxy, user: str
) -> None:
    elsewhere = listener_client(instance, ListenerName.LOCAL, address="10.8.0.7")
    sign_in(elsewhere, USER, PASSWORD)
    assert elsewhere.get("/api/me").status_code == 200

    activate(local, a_link_to_reset(tunnel, proxy), NEW_PASSWORD)

    assert elsewhere.get("/api/me").status_code == 401
    assert local.get("/api/me").json()["email"] == USER


def test_a_link_to_reset_works_once(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy, user: str
) -> None:
    link = a_link_to_reset(tunnel, proxy)
    activate(local, link, NEW_PASSWORD)
    local.post("/sign-out")

    assert_the_link_no_longer_works(local, link)
    assert sign_in(local, USER, NEW_PASSWORD).url.path == "/"


def test_a_link_to_reset_expires_after_seven_days(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy, clock: FakeClock, user: str
) -> None:
    link = a_link_to_reset(tunnel, proxy)

    clock.advance(days=7, seconds=-1)
    assert local.get(link).status_code == 200
    clock.advance(seconds=1)

    assert_the_link_no_longer_works(local, link)
    assert sign_in(local, USER, PASSWORD).url.path == "/"


def test_a_new_link_to_reset_voids_the_previous_one(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy, user: str
) -> None:
    previous = a_link_to_reset(tunnel, proxy)

    link = a_link_to_reset(tunnel, proxy)

    assert link != previous
    assert_the_link_no_longer_works(local, previous)
    assert activate(local, link, NEW_PASSWORD).url.path == "/"


def test_removing_the_user_voids_their_new_link_to_reset(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy, user: str
) -> None:
    link = a_link_to_reset(tunnel, proxy)

    tunnel.delete(f"/api/users/{USER}", headers=proxy.headers(ADMINISTRATOR))

    assert_the_link_no_longer_works(local, link)
    invited = tunnel.post(
        "/api/invitations", json={"email": USER}, headers=proxy.headers(ADMINISTRATOR)
    )
    assert invited.status_code == 201  # invited again: still not with the old link
    assert_the_link_no_longer_works(local, link)


def test_revoking_an_invitation_never_voids_a_users_new_link_to_reset(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy, user: str
) -> None:
    link = a_link_to_reset(tunnel, proxy)

    revoked = tunnel.delete(f"/api/invitations/{USER}", headers=proxy.headers(ADMINISTRATOR))

    assert revoked.status_code == 404
    assert activate(local, link, NEW_PASSWORD).url.path == "/"


def test_a_link_to_reset_is_only_for_a_user(
    tunnel: TestClient, proxy: AccessProxy, user: str
) -> None:
    tunnel.post(
        "/api/invitations",
        json={"email": "pending@example.com"},
        headers=proxy.headers(ADMINISTRATOR),
    )

    assert new_link_to_reset(tunnel, proxy, "nobody@example.com").status_code == 404
    assert new_link_to_reset(tunnel, proxy, "pending@example.com").status_code == 404
    assert new_link_to_reset(tunnel, proxy, USER.upper()).status_code == 201


def test_without_local_sign_in_there_are_no_links_to_reset_to_make(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    assert new_link_to_reset(dockyard, proxy, ADMINISTRATOR).status_code == 404


def test_only_administrators_make_links_to_reset(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy, user: str
) -> None:
    refused = new_link_to_reset(tunnel, proxy, USER, by=USER)

    assert refused.status_code == 403
    assert refused.json()["error"] == "administrator_only"
    assert (
        tunnel.post(f"/users/{USER}/activation-link", headers=proxy.headers(USER)).status_code
        == 403
    )
    sign_in(local, USER, PASSWORD)
    assert local.post(f"/api/users/{USER}/activation-link").status_code == 403


def test_the_users_page_makes_a_link_to_reset_to_copy(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy, user: str
) -> None:
    tunnel.headers.update(proxy.headers(ADMINISTRATOR))
    assert f"New Activation link for {USER}" in tunnel.get("/users").text

    made = tunnel.post(f"/users/{USER}/activation-link")

    assert made.status_code == 201
    assert f"Activation link for {USER}" in made.text
    assert "signs them out everywhere" in made.text
    link = link_on_page(made.text)
    assert link.startswith("https://192.168.1.10:8443/activate/")
    assert "/activate/" not in tunnel.get("/users").text  # shown once, as it is made
    assert activate(local, path_of(link), NEW_PASSWORD).url.path == "/"
    assert tunnel.post("/users/nobody@example.com/activation-link").status_code == 404


def test_without_local_sign_in_the_users_page_has_no_links_to_reset(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    dockyard.headers.update(proxy.headers(ADMINISTRATOR))

    assert "New Activation link" not in dockyard.get("/users").text
    assert dockyard.post(f"/users/{ADMINISTRATOR}/activation-link").status_code == 404


def test_the_link_to_reset_asks_for_a_new_password_and_tells_what_follows(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy, user: str
) -> None:
    page = local.get(a_link_to_reset(tunnel, proxy))

    assert page.status_code == 200
    assert "Choose a new password" in page.text
    assert "signed out everywhere else" in page.text
    assert "You are invited" not in page.text
    assert "At least 12 characters, and not your email." in page.text


def test_a_new_password_that_breaks_the_rules_is_refused_and_the_old_one_still_works(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy, user: str
) -> None:
    link = a_link_to_reset(tunnel, proxy)

    refused = activate(local, link, "eleven char")

    assert refused.status_code == 422
    assert "Choose a new password" in refused.text
    assert "The password is too short" in refused.text
    assert sign_in(local, USER, PASSWORD).url.path == "/"


def test_resetting_speaks_italian(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy, user: str
) -> None:
    tunnel.headers.update({**proxy.headers(ADMINISTRATOR), "Accept-Language": "it"})
    local.headers["Accept-Language"] = "it"

    made = tunnel.post(f"/users/{USER}/activation-link")

    assert f"Nuovo Link di attivazione per {USER}" in tunnel.get("/users").text
    assert "chiude tutte le sue sessioni di accesso" in made.text
    page = local.get(path_of(link_on_page(made.text)))
    assert "Scegli una nuova password" in page.text
    assert "altre sessioni di accesso vengono chiuse" in page.text


def test_the_tunnel_listener_never_opens_a_link_to_reset(
    tunnel: TestClient, local: TestClient, proxy: AccessProxy, user: str
) -> None:
    link = a_link_to_reset(tunnel, proxy)
    form = {"password": NEW_PASSWORD, "repeated": NEW_PASSWORD}

    assert tunnel.get(link, headers=proxy.headers(USER)).status_code == 404
    assert tunnel.post(link, data=form, headers=proxy.headers(USER)).status_code == 404
    assert sign_in(local, USER, PASSWORD).url.path == "/"
