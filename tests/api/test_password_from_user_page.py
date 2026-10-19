"""A User's password from their own page: set one when they have none, from either listener;
change it by giving the current one, on the local listener only (ADR 0004: the tunnel never
checks a password). Only while Local sign-in is on."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dockyard.web import Instance, ListenerName

from .conftest import ADMINISTRATOR, AccessProxy, FakeClock, listener_client
from .test_local_sign_in import LOCAL_SIGN_IN, ON_THE_LAN, PASSWORD, sign_in

USER = "cloudflare.user@example.com"
NEW_PASSWORD = "a brand new password of mine"


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
def user(tunnel: TestClient, proxy: AccessProxy) -> dict[str, str]:
    """A User who came in through Cloudflare: the headers of their requests."""
    administrator = proxy.headers(ADMINISTRATOR)
    tunnel.post("/api/invitations", json={"email": USER}, headers=administrator)
    headers = proxy.headers(USER)
    assert tunnel.get("/api/me", headers=headers).status_code == 200
    return headers


def set_password(
    client: TestClient, password: str, *, current: str | None = None, **headers: Any
) -> Any:
    form = {"password": password, "repeated": password}
    if current is not None:
        form["current"] = current
    return client.post("/me/password", data=form, **headers)


def test_a_user_who_came_through_cloudflare_sets_a_password_and_signs_in_on_the_lan(
    tunnel: TestClient, local: TestClient, user: dict[str, str]
) -> None:
    page = tunnel.get("/me", headers=user)
    assert page.status_code == 200
    assert "You have no password yet" in page.text

    set_ = set_password(tunnel, PASSWORD, headers=user)

    assert set_.status_code == 200
    assert set_.url.path == "/me"
    assert "You have a password" in set_.text
    assert sign_in(local, USER, PASSWORD).url.path == "/"
    assert local.get("/api/me").json()["email"] == USER


@pytest.fixture
def administrator(instance: Instance, local: TestClient) -> TestClient:
    """The first Administrator, given a password, signed in on the local listener."""
    instance.local_sign_in.set_password(ADMINISTRATOR, PASSWORD)
    sign_in(local, ADMINISTRATOR, PASSWORD)
    return local


def test_a_user_with_a_password_changes_it_by_giving_the_current_one(
    administrator: TestClient, start_instance: Callable[..., Instance]
) -> None:
    page = administrator.get("/me")
    assert "You have a password" in page.text
    assert 'name="current"' in page.text

    changed = set_password(administrator, NEW_PASSWORD, current=PASSWORD)

    assert changed.status_code == 200
    assert "Your password is saved." in changed.text
    other = listener_client(start_instance(**LOCAL_SIGN_IN), ListenerName.LOCAL, address=ON_THE_LAN)
    assert sign_in(other, ADMINISTRATOR, PASSWORD).status_code == 401
    assert sign_in(other, ADMINISTRATOR, NEW_PASSWORD).url.path == "/"


@pytest.mark.parametrize("current", [None, "", "a wrong password, long enough"])
def test_a_password_is_not_changed_without_the_current_one(
    administrator: TestClient, local: TestClient, current: str | None
) -> None:
    refused = set_password(administrator, NEW_PASSWORD, current=current)

    assert refused.status_code == 403
    assert "The current password is wrong." in refused.text
    local.post("/sign-out")
    assert sign_in(local, ADMINISTRATOR, NEW_PASSWORD).status_code == 401
    assert sign_in(local, ADMINISTRATOR, PASSWORD).url.path == "/"


WRONG = "a wrong password, long enough"


def test_wrong_current_passwords_slow_down_changes_and_sign_ins_alike(
    administrator: TestClient, local: TestClient, clock: FakeClock
) -> None:
    for _ in range(5):
        assert set_password(administrator, NEW_PASSWORD, current=WRONG).status_code == 403
    clock.advance(seconds=29)

    slowed_down = set_password(administrator, NEW_PASSWORD, current=PASSWORD)

    assert slowed_down.status_code == 429
    assert slowed_down.headers["retry-after"] == "1"
    assert "Too many failed attempts. Try again in 1 s." in slowed_down.text
    local.post("/sign-out")
    assert sign_in(local, ADMINISTRATOR, PASSWORD).status_code == 429
    clock.advance(seconds=1)
    assert sign_in(local, ADMINISTRATOR, PASSWORD).url.path == "/"
    assert set_password(local, NEW_PASSWORD, current=PASSWORD).status_code == 200


def test_a_slowed_down_change_says_when_to_try_again_whatever_the_new_password(
    administrator: TestClient,
) -> None:
    for _ in range(5):
        set_password(administrator, NEW_PASSWORD, current=WRONG)

    slowed_down = set_password(administrator, "too short", current=PASSWORD)

    assert slowed_down.status_code == 429
    assert "Too many failed attempts. Try again in 30 s." in slowed_down.text


def test_through_the_tunnel_a_password_is_never_checked_so_it_changes_only_on_the_lan(
    instance: Instance, tunnel: TestClient, local: TestClient, user: dict[str, str]
) -> None:
    instance.local_sign_in.set_password(USER, PASSWORD)

    page = tunnel.get("/me", headers=user)
    assert "You have a password" in page.text
    assert "open this page on the local address" in page.text
    assert 'name="current"' not in page.text
    refused = set_password(tunnel, NEW_PASSWORD, current=PASSWORD, headers=user)
    assert refused.status_code == 409
    assert "it changes only by giving it, on the local address" in refused.text
    for _ in range(5):
        set_password(tunnel, NEW_PASSWORD, current=WRONG, headers=user)
    assert sign_in(local, USER, PASSWORD).url.path == "/"  # unchanged, and not slowed down


def test_changing_the_password_signs_out_every_other_session_and_keeps_this_one(
    instance: Instance, administrator: TestClient
) -> None:
    elsewhere = listener_client(instance, ListenerName.LOCAL, address="192.168.1.30")
    sign_in(elsewhere, ADMINISTRATOR, PASSWORD)
    assert elsewhere.get("/api/me").status_code == 200

    set_password(administrator, NEW_PASSWORD, current=PASSWORD)

    assert administrator.get("/api/me").json()["email"] == ADMINISTRATOR
    assert elsewhere.get("/api/me").status_code == 401


@pytest.mark.parametrize(
    ("password", "repeated", "reason"),
    [
        ("eleven char", "eleven char", "too short: it needs at least 12 characters."),
        (USER.upper(), USER.upper(), "The password cannot be your email."),
        (PASSWORD, NEW_PASSWORD, "The two passwords are different."),
    ],
    ids=["too-short", "equals-email", "different"],
)
def test_a_password_that_breaks_the_rules_is_refused_and_explained(
    tunnel: TestClient, user: dict[str, str], password: str, repeated: str, reason: str
) -> None:
    refused = tunnel.post(
        "/me/password", data={"password": password, "repeated": repeated}, headers=user
    )

    assert refused.status_code == 422
    assert reason in refused.text
    assert "You have no password yet" in refused.text


def test_the_page_offers_to_set_a_password_and_explains_the_rules(
    tunnel: TestClient, user: dict[str, str]
) -> None:
    page = tunnel.get("/me", headers=user)

    assert 'href="/me"' in page.text  # the email at the top leads here, from every page
    assert "At least 12 characters, and not your email." in page.text
    assert 'name="current"' not in page.text
    assert "Set the password" in page.text


def test_the_page_speaks_italian(tunnel: TestClient, user: dict[str, str]) -> None:
    tunnel.cookies.set("dockyard_lang", "it")

    page = tunnel.get("/me", headers=user)

    assert "La tua pagina" in page.text
    assert "Non hai ancora una password" in page.text
    assert "Almeno 12 caratteri, e non la tua email." in page.text


def test_without_local_sign_in_the_page_has_no_password_section(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    administrator = proxy.headers(ADMINISTRATOR)

    page = dockyard.get("/me", headers=administrator)

    assert page.status_code == 200
    assert ADMINISTRATOR in page.text
    assert "Password" not in page.text
    refused = set_password(dockyard, PASSWORD, headers=administrator)
    assert refused.status_code == 404
