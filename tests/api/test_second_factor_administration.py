"""Administering the Second factor: the Users page says who has a password and who has a Second
factor, and an Administrator removes the Second factor of a User who lost their phone and their
recovery codes. The User then signs in with the password alone, and can turn it on again."""

import re
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dockyard.web import Instance, ListenerName

from .conftest import ADMINISTRATOR, AccessProxy, FakeClock, listener_client
from .test_local_sign_in import LOCAL_SIGN_IN, ON_THE_LAN, PASSWORD, USER, sign_in
from .test_second_factor import authenticator_code, give_code, turn_on


@pytest.fixture
def instance(start_instance: Callable[..., Instance], proxy: AccessProxy) -> Instance:
    """The first Administrator, with a password, and a User who has none yet."""
    started = start_instance(**LOCAL_SIGN_IN)
    started.local_sign_in.set_password(ADMINISTRATOR, PASSWORD)
    tunnel = listener_client(started, ListenerName.TUNNEL)
    tunnel.post("/api/invitations", json={"email": USER}, headers=proxy.headers(ADMINISTRATOR))
    tunnel.get("/api/me", headers=proxy.headers(USER))
    return started


def signed_in_locally(instance: Instance, email: str) -> TestClient:
    client = listener_client(instance, ListenerName.LOCAL, address=ON_THE_LAN)
    assert sign_in(client, email, PASSWORD).url.path == "/"
    return client


@pytest.fixture
def administrator(instance: Instance) -> TestClient:
    """The first Administrator, signed in on the local listener."""
    return signed_in_locally(instance, ADMINISTRATOR)


@pytest.fixture
def user(instance: Instance, clock: FakeClock) -> str:
    """The User, with a password and a Second factor; the secret of their authenticator app."""
    instance.local_sign_in.set_password(USER, PASSWORD)
    secret, _ = turn_on(signed_in_locally(instance, USER), clock)
    return secret


def row_of(page: str, email: str) -> str:
    """What the Users page lists for that User."""
    found = re.search(rf'<li data-user="{re.escape(email)}">(.*?)</li>', page, re.DOTALL)
    assert found, page
    return found.group(1)


def test_the_users_page_says_who_has_a_password_and_who_has_a_second_factor(
    instance: Instance, administrator: TestClient, clock: FakeClock
) -> None:
    page = administrator.get("/users").text
    assert "No password" not in row_of(page, ADMINISTRATOR)
    assert ">Password<" in row_of(page, ADMINISTRATOR)
    assert "No Second factor" in row_of(page, ADMINISTRATOR)
    assert "No password" in row_of(page, USER)
    assert "No Second factor" in row_of(page, USER)

    instance.local_sign_in.set_password(USER, PASSWORD)
    turn_on(signed_in_locally(instance, USER), clock)

    listed = row_of(administrator.get("/users").text, USER)
    assert "No password" not in listed
    assert ">Password<" in listed
    assert "No Second factor" not in listed
    assert "Second factor" in listed


def test_without_local_sign_in_the_users_page_says_nothing_of_passwords(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    dockyard.headers.update(proxy.headers(ADMINISTRATOR))

    page = dockyard.get("/users").text

    assert "No password" not in page
    assert "Second factor" not in page


def remove_from_page(client: TestClient, email: str = USER) -> Any:
    return client.post(f"/users/{email}/second-factor/remove")


def test_an_administrator_removes_a_users_second_factor_and_the_password_alone_signs_in(
    instance: Instance, administrator: TestClient, user: str, clock: FakeClock
) -> None:
    listed = row_of(administrator.get("/users").text, USER)
    assert f"Remove the Second factor of {USER}" in listed

    removed = remove_from_page(administrator)

    assert removed.status_code == 200
    assert f"The Second factor of {USER} is removed" in removed.text
    assert "No Second factor" in row_of(removed.text, USER)
    assert f"Remove the Second factor of {USER}" not in removed.text
    browser = listener_client(instance, ListenerName.LOCAL, address=ON_THE_LAN)
    assert sign_in(browser, USER, PASSWORD).url.path == "/"  # no code asked for
    assert "You have no Second factor" in browser.get("/me").text


def test_the_user_can_turn_it_on_again(
    instance: Instance, administrator: TestClient, user: str, clock: FakeClock
) -> None:
    remove_from_page(administrator)
    browser = signed_in_locally(instance, USER)

    secret, _ = turn_on(browser, clock)

    browser.post("/sign-out")
    asked = sign_in(browser, USER, PASSWORD)
    assert asked.url.path == "/sign-in/code"
    assert give_code(browser, authenticator_code(secret, clock.now())).url.path == "/"


def test_removing_it_ends_the_code_steps_halfway(
    instance: Instance, administrator: TestClient, user: str, clock: FakeClock
) -> None:
    """Someone at the code step with the password keeps no way to finish it with old codes."""
    browser = listener_client(instance, ListenerName.LOCAL, address=ON_THE_LAN)
    assert sign_in(browser, USER, PASSWORD).url.path == "/sign-in/code"

    remove_from_page(administrator)

    over = give_code(browser, authenticator_code(user, clock.now()))
    assert over.status_code == 401
    assert "Sign in again" in over.text
    assert browser.get("/api/me").status_code == 401


def test_the_api_removes_it_too(instance: Instance, user: str, proxy: AccessProxy) -> None:
    tunnel = listener_client(instance, ListenerName.TUNNEL)
    tunnel.headers.update(proxy.headers(ADMINISTRATOR))

    assert tunnel.delete(f"/api/users/{USER}/second-factor").status_code == 204
    assert not instance.local_sign_in.has_second_factor(USER)
    assert tunnel.delete(f"/api/users/{USER}/second-factor").status_code == 404  # none left
    assert tunnel.delete("/api/users/nobody@example.com/second-factor").status_code == 404


def test_the_email_may_be_given_in_any_case(administrator: TestClient, user: str) -> None:
    assert remove_from_page(administrator, USER.upper()).status_code == 200
    assert "No Second factor" in row_of(administrator.get("/users").text, USER)


def test_there_is_nothing_to_remove_from_an_unknown_user_or_one_without_it(
    administrator: TestClient,
) -> None:
    assert remove_from_page(administrator, "nobody@example.com").status_code == 404
    assert remove_from_page(administrator, USER).status_code == 404


def test_only_administrators_remove_a_second_factor(
    instance: Instance, administrator: TestClient, clock: FakeClock, proxy: AccessProxy
) -> None:
    turn_on(administrator, clock)
    tunnel = listener_client(instance, ListenerName.TUNNEL)
    tunnel.headers.update(proxy.headers(USER))

    refused = remove_from_page(tunnel, ADMINISTRATOR)
    assert refused.status_code == 403
    refused = tunnel.delete(f"/api/users/{ADMINISTRATOR}/second-factor")
    assert refused.status_code == 403
    assert refused.json()["error"] == "administrator_only"

    assert instance.local_sign_in.has_second_factor(ADMINISTRATOR)


def test_an_administrator_removes_it_through_cloudflare_too(
    instance: Instance, user: str, proxy: AccessProxy
) -> None:
    """Like making Activation links: administering it is not signing in with it."""
    tunnel = listener_client(instance, ListenerName.TUNNEL)
    tunnel.headers.update(proxy.headers(ADMINISTRATOR))
    assert f"Remove the Second factor of {USER}" in tunnel.get("/users").text

    assert remove_from_page(tunnel).status_code == 200
    assert not instance.local_sign_in.has_second_factor(USER)


def test_without_local_sign_in_there_is_no_second_factor_to_remove(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    dockyard.headers.update(proxy.headers(ADMINISTRATOR))

    assert remove_from_page(dockyard, ADMINISTRATOR).status_code == 404
    assert dockyard.delete(f"/api/users/{ADMINISTRATOR}/second-factor").status_code == 404


def test_administering_the_second_factor_speaks_italian(
    administrator: TestClient, user: str
) -> None:
    administrator.headers["Accept-Language"] = "it"
    page = administrator.get("/users").text
    assert "Nessuna password" not in row_of(page, USER)
    assert "Secondo fattore" in row_of(page, USER)
    assert f"Rimuovi il Secondo fattore di {USER}" in page

    removed = remove_from_page(administrator)

    assert f"Il Secondo fattore di {USER} è rimosso" in removed.text
    assert "Nessun Secondo fattore" in row_of(removed.text, USER)


def test_an_administrator_turns_off_their_own_only_from_their_page(
    instance: Instance, administrator: TestClient, clock: FakeClock, proxy: AccessProxy
) -> None:
    """With a code or the password, which someone at their unlocked device has not."""
    turn_on(administrator, clock)
    assert f"Remove the Second factor of {ADMINISTRATOR}" not in administrator.get("/users").text

    refused = remove_from_page(administrator, ADMINISTRATOR)
    assert refused.status_code == 403
    assert "from your own page" in refused.text
    tunnel = listener_client(instance, ListenerName.TUNNEL)
    refused = tunnel.delete(
        f"/api/users/{ADMINISTRATOR}/second-factor", headers=proxy.headers(ADMINISTRATOR)
    )
    assert refused.status_code == 403
    assert refused.json()["error"] == "own_second_factor"

    assert instance.local_sign_in.has_second_factor(ADMINISTRATOR)
