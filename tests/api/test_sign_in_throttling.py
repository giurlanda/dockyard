"""Slowing down failed sign-ins: nobody on the LAN guesses a password by trying many times, and
nobody keeps a User out for good by failing on purpose."""

import logging
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from dockyard.identity import SESSION_COOKIE
from dockyard.web import Instance, ListenerName

from .conftest import ADMINISTRATOR, FakeClock, listener_client
from .test_local_sign_in import LOCAL_SIGN_IN, ON_THE_LAN, PASSWORD, sign_in

WRONG = "a wrong password, long enough"


@pytest.fixture
def instance(start_instance: Callable[..., Instance]) -> Instance:
    started = start_instance(**LOCAL_SIGN_IN)
    started.local_sign_in.set_password(ADMINISTRATOR, PASSWORD)
    return started


@pytest.fixture
def local(instance: Instance) -> TestClient:
    return listener_client(instance, ListenerName.LOCAL, address=ON_THE_LAN)


def fail(local: TestClient, times: int, email: str = ADMINISTRATOR) -> None:
    for _ in range(times):
        assert sign_in(local, email, WRONG).status_code == 401


def test_after_five_failures_on_an_email_even_the_right_password_waits_30_seconds(
    local: TestClient, clock: FakeClock
) -> None:
    fail(local, 5)
    clock.advance(seconds=29)

    slowed_down = sign_in(local, ADMINISTRATOR, PASSWORD)

    assert slowed_down.status_code == 429
    assert slowed_down.headers["retry-after"] == "1"
    assert "Too many failed attempts. Try again in 1 s." in slowed_down.text
    assert SESSION_COOKIE not in local.cookies
    clock.advance(seconds=1)
    assert sign_in(local, ADMINISTRATOR, PASSWORD).url.path == "/"


def test_each_further_failure_doubles_the_delay_up_to_15_minutes(
    local: TestClient, clock: FakeClock
) -> None:
    fail(local, 4)

    for delay, told in [
        (30, "30 s"),
        (60, "1 min"),
        (120, "2 min"),
        (240, "4 min"),
        (480, "8 min"),
        (900, "15 min"),
        (900, "15 min"),
    ]:
        failed = sign_in(local, ADMINISTRATOR, WRONG)
        assert failed.status_code == 401
        assert "The email or the password is wrong." in failed.text
        assert f"Too many failed attempts. Try again in {told}." in failed.text
        assert failed.headers["retry-after"] == str(delay)
        clock.advance(seconds=delay - 1)
        assert sign_in(local, ADMINISTRATOR, WRONG).status_code == 429
        clock.advance(seconds=1)

    assert sign_in(local, ADMINISTRATOR, PASSWORD).url.path == "/"


def test_the_email_waits_whatever_address_it_is_tried_from(
    instance: Instance, local: TestClient
) -> None:
    fail(local, 5)
    elsewhere = listener_client(instance, ListenerName.LOCAL, address="192.168.1.30")

    assert sign_in(elsewhere, ADMINISTRATOR, PASSWORD).status_code == 429


def test_an_address_failing_on_many_emails_waits_for_every_email(
    instance: Instance, local: TestClient, clock: FakeClock
) -> None:
    for attempt in range(5):
        fail(local, 1, email=f"guess.{attempt}@example.com")

    assert sign_in(local, ADMINISTRATOR, PASSWORD).status_code == 429
    elsewhere = listener_client(instance, ListenerName.LOCAL, address="192.168.1.30")
    assert sign_in(elsewhere, ADMINISTRATOR, PASSWORD).url.path == "/"
    clock.advance(seconds=30)
    assert sign_in(local, ADMINISTRATOR, PASSWORD).url.path == "/"


def test_a_wrong_email_is_slowed_down_and_told_so_exactly_like_a_wrong_password(
    instance: Instance,
) -> None:
    def refusals(email: str, address: str) -> list[str]:
        client = listener_client(instance, ListenerName.LOCAL, address=address)
        fail(client, 4, email=email)
        failed = sign_in(client, email, WRONG)
        slowed_down = sign_in(client, email, WRONG)
        return [
            f"{response.status_code} {response.headers['retry-after']} "
            + response.text.replace(email, "")
            for response in (failed, slowed_down)
        ]

    assert refusals("nobody@example.com", "192.168.1.30") == refusals(ADMINISTRATOR, "192.168.1.31")


def test_a_successful_sign_in_starts_the_count_of_its_email_again(
    instance: Instance, local: TestClient
) -> None:
    fail(local, 4)
    sign_in(local, ADMINISTRATOR, PASSWORD)
    local.cookies.clear()
    elsewhere = listener_client(instance, ListenerName.LOCAL, address="192.168.1.30")

    fail(elsewhere, 4)

    assert sign_in(elsewhere, ADMINISTRATOR, PASSWORD).url.path == "/"


def test_every_failure_is_logged_with_the_email_and_the_client_address(
    local: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)

    fail(local, 5, email="Nobody@Example.com")
    sign_in(local, ADMINISTRATOR, PASSWORD)

    failures = [record.getMessage() for record in caplog.records if "sign-in" in record.message]
    assert failures == [
        *["Local sign-in failed for 'nobody@example.com' from 192.168.1.20"] * 5,
        f"Local sign-in slowed down for '{ADMINISTRATOR}' from 192.168.1.20",
    ]


def test_the_wait_is_told_in_italian_to_whoever_asks(local: TestClient) -> None:
    local.headers["Accept-Language"] = "it-IT,it;q=0.9"
    fail(local, 5)

    slowed_down = sign_in(local, ADMINISTRATOR, PASSWORD)

    assert "Troppi tentativi falliti. Riprova tra 30 s." in slowed_down.text


def test_failures_a_day_old_no_longer_count(local: TestClient, clock: FakeClock) -> None:
    """Otherwise the typing mistakes of months would add up to a delay at the next one."""
    fail(local, 4)
    clock.advance(days=1)

    failed = sign_in(local, ADMINISTRATOR, WRONG)

    assert "retry-after" not in failed.headers
