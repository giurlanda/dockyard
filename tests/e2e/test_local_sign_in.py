"""Local sign-in in a browser: the first Administrator signs in on the local listener, over HTTPS
with the self-signed certificate Dockyard made, and signs out."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from playwright.sync_api import Browser, Page, expect

from dockyard.runtime.fake import FakeRuntime
from dockyard.serving import Servers
from dockyard.settings import Settings
from dockyard.web import create_instance

from ..api.test_second_factor import authenticator_code
from .conftest import ADMINISTRATOR, PHONE, fits_the_screen, free_port

PASSWORD = "correct horse battery staple"


@pytest.fixture
def local_url(tmp_path: Path, runtime: FakeRuntime) -> Iterator[str]:
    """Dockyard with Local sign-in only, reached from the loopback as from the LAN."""
    port = free_port()
    settings = Settings.from_env(
        {
            "DOCKYARD_SECRET_KEY": Fernet.generate_key().decode(),
            "DOCKYARD_FIRST_ADMINISTRATOR_EMAIL": ADMINISTRATOR,
            "DOCKYARD_DATA_DIR": str(tmp_path / "dockyard-data"),
            "DOCKYARD_LOCAL_PORT": str(port),
            "DOCKYARD_LOCAL_ADDRESS": "127.0.0.1",
            "DOCKYARD_LOCAL_NETWORKS": "127.0.0.0/8",
        }
    )
    instance = create_instance(settings, runtime)
    instance.local_sign_in.set_password(ADMINISTRATOR, PASSWORD)  # as set-password does
    servers = Servers(instance.listeners.values(), log_level="warning")
    thread = threading.Thread(target=servers.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not servers.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("Dockyard did not start")
        time.sleep(0.05)
    yield f"https://127.0.0.1:{port}"
    servers.stop()
    thread.join(timeout=10)


@pytest.fixture
def open_local(browser: Browser, local_url: str) -> Iterator[Page]:
    # The certificate is self-signed: the person accepts it once, the test ignores it.
    context = browser.new_context(base_url=local_url, ignore_https_errors=True, locale="en-US")
    yield context.new_page()
    context.close()


def test_the_first_administrator_signs_in_sees_the_dashboard_and_signs_out(
    open_local: Page,
) -> None:
    page = open_local
    page.goto("/")

    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
    page.get_by_label("Email").fill(ADMINISTRATOR)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()

    expect(page.get_by_test_id("user-email")).to_have_text(ADMINISTRATOR)
    expect(page.get_by_role("heading", name="Projects")).to_be_visible()

    page.get_by_role("button", name="Sign out").click()
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
    page.goto("/")
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()


def test_a_wrong_password_is_refused_and_the_email_stays(open_local: Page) -> None:
    page = open_local
    page.goto("/sign-in")
    page.get_by_label("Email").fill(ADMINISTRATOR)
    page.get_by_label("Password").fill("a wrong password, long enough")
    page.get_by_role("button", name="Sign in").click()

    expect(page.get_by_role("alert")).to_have_text("The email or the password is wrong.")
    expect(page.get_by_label("Email")).to_have_value(ADMINISTRATOR)


def test_the_sign_in_page_switches_to_italian(open_local: Page) -> None:
    page = open_local
    page.goto("/sign-in")

    page.get_by_role("button", name="Italiano").click()

    expect(page.get_by_role("heading", name="Accedi")).to_be_visible()
    page.get_by_label("Email").fill(ADMINISTRATOR)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Accedi").click()
    expect(page.get_by_role("button", name="Esci")).to_be_visible()


@pytest.mark.parametrize("language", ["English", "Italiano"])
def test_the_sign_in_page_fits_a_phone_screen(
    browser: Browser, local_url: str, language: str
) -> None:
    context = browser.new_context(
        base_url=local_url,
        ignore_https_errors=True,
        locale="en-US",
        viewport=PHONE,  # type: ignore[arg-type]
    )
    page = context.new_page()
    page.goto("/sign-in")
    page.get_by_role("button", name=language).click()
    expect(page.get_by_role("button", name=language)).to_have_attribute("aria-pressed", "true")
    try:
        assert fits_the_screen(page)
        page.get_by_label("Email").fill(ADMINISTRATOR)
        page.get_by_label("Password").fill(PASSWORD)
        page.get_by_label("Password").press("Enter")
        expect(page.get_by_test_id("user-email")).to_have_text(ADMINISTRATOR)
        assert fits_the_screen(page)
    finally:
        context.close()


INVITED = "invited.person@dockyard.example.com"
INVITED_PASSWORD = "a password of my own choosing"


def sign_in(page: Page, email: str, password: str) -> None:
    page.goto("/sign-in")
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Sign in").click()


def invite_and_copy_the_activation_link(page: Page) -> str:
    """As the Administrator: invite the person, copy their Activation link, return it."""
    sign_in(page, ADMINISTRATOR, PASSWORD)
    page.get_by_role("link", name="Users and Invitations").click()
    page.get_by_label("Email").fill(INVITED)
    page.get_by_role("button", name="Invite").click()
    page.get_by_role("button", name="Copy").click()
    expect(page.get_by_role("status")).to_have_text("Copied.")
    link: str = page.evaluate("navigator.clipboard.readText()")
    expect(page.get_by_role("textbox", name=f"Activation link for {INVITED}")).to_have_value(link)
    return link


def test_an_invited_person_chooses_a_password_with_the_activation_link_and_lands_on_their_dashboard(
    browser: Browser, local_url: str
) -> None:
    administrator = browser.new_context(
        base_url=local_url,
        ignore_https_errors=True,
        locale="en-US",
        permissions=["clipboard-read", "clipboard-write"],
    )
    person = browser.new_context(ignore_https_errors=True, locale="en-US")
    try:
        link = invite_and_copy_the_activation_link(administrator.new_page())

        page = person.new_page()
        page.goto(link)
        expect(page.get_by_role("heading", name="Choose your password")).to_be_visible()
        expect(page.get_by_text("At least 12 characters, and not your email.")).to_be_visible()
        page.get_by_label("Password", exact=True).fill(INVITED_PASSWORD)
        page.get_by_label("Repeat the password").fill(INVITED_PASSWORD)
        page.get_by_role("button", name="Set the password and sign in").click()

        expect(page.get_by_test_id("user-email")).to_have_text(INVITED)
        expect(page.get_by_role("heading", name="Projects")).to_be_visible()
        page.goto(link)
        expect(page.get_by_text("Ask an Administrator for a new one.")).to_be_visible()
    finally:
        administrator.close()
        person.close()


@pytest.mark.parametrize("language", ["English", "Italiano"])
def test_the_activation_page_fits_a_phone_screen(
    browser: Browser, local_url: str, language: str
) -> None:
    administrator = browser.new_context(
        base_url=local_url,
        ignore_https_errors=True,
        locale="en-US",
        viewport=PHONE,  # type: ignore[arg-type]
        permissions=["clipboard-read", "clipboard-write"],
    )
    person = browser.new_context(
        ignore_https_errors=True,
        locale="en-US",
        viewport=PHONE,  # type: ignore[arg-type]
    )
    try:
        page = administrator.new_page()
        link = invite_and_copy_the_activation_link(page)
        assert fits_the_screen(page)

        page = person.new_page()
        page.goto(link)
        page.get_by_role("button", name=language).click()
        expect(page.get_by_role("button", name=language)).to_have_attribute("aria-pressed", "true")
        assert fits_the_screen(page)
        page.locator("#password").fill(INVITED_PASSWORD)
        page.locator("#repeated").fill(INVITED_PASSWORD)
        page.locator("#repeated").press("Enter")
        expect(page.get_by_test_id("user-email")).to_have_text(INVITED)
    finally:
        administrator.close()
        person.close()


NEW_PASSWORD = "a brand new password of mine"


@pytest.mark.parametrize(
    ("language", "saved"),
    [("English", "Your password is saved."), ("Italiano", "La password è salvata.")],
)
def test_a_user_changes_their_password_from_their_page_on_a_phone(
    browser: Browser, local_url: str, language: str, saved: str
) -> None:
    context = browser.new_context(
        base_url=local_url,
        ignore_https_errors=True,
        locale="en-US",
        viewport=PHONE,  # type: ignore[arg-type]
    )
    page = context.new_page()
    try:
        sign_in(page, ADMINISTRATOR, PASSWORD)
        page.get_by_role("button", name=language).click()
        expect(page.get_by_role("button", name=language)).to_have_attribute("aria-pressed", "true")
        page.get_by_test_id("user-email").click()
        assert fits_the_screen(page)
        page.locator("#current").fill(PASSWORD)
        page.locator("#password").fill(NEW_PASSWORD)
        page.locator("#repeated").fill(NEW_PASSWORD)
        page.locator("#repeated").press("Enter")
        expect(page.get_by_role("status")).to_contain_text(saved)
        expect(page.locator("#current")).to_be_visible()
        assert fits_the_screen(page)
    finally:
        context.close()


@pytest.mark.parametrize("language", ["English", "Italiano"])
def test_a_user_turns_on_the_second_factor_and_signs_in_again_with_a_code_on_a_phone(
    browser: Browser, local_url: str, language: str
) -> None:
    context = browser.new_context(
        base_url=local_url,
        ignore_https_errors=True,
        locale="en-US",
        viewport=PHONE,  # type: ignore[arg-type]
    )
    page = context.new_page()
    try:
        sign_in(page, ADMINISTRATOR, PASSWORD)
        page.get_by_role("button", name=language).click()
        expect(page.get_by_role("button", name=language)).to_have_attribute("aria-pressed", "true")
        page.get_by_test_id("user-email").click()
        page.locator("form[action='/me/second-factor'] button").click()

        expect(page.locator(".qr-code svg")).to_be_visible()
        assert fits_the_screen(page)
        secret = page.locator("#second-factor-secret").inner_text()
        page.locator("#code").fill(authenticator_code(secret, time.time()))
        page.locator("#code").press("Enter")
        expect(page.locator(".recovery-code")).to_have_count(10)
        assert fits_the_screen(page)

        page.locator("form[action='/sign-out'] button").click()
        page.locator("#email").fill(ADMINISTRATOR)
        page.locator("#password").fill(PASSWORD)
        page.locator("#password").press("Enter")
        expect(page.locator("#code")).to_be_visible()
        assert fits_the_screen(page)
        # The next step's code: the one that confirmed the Second factor works no more.
        page.locator("#code").fill(authenticator_code(secret, time.time() + 30))
        page.locator("#code").press("Enter")
        expect(page.get_by_test_id("user-email")).to_have_text(ADMINISTRATOR)
        page.get_by_test_id("user-email").click()
        expect(page.locator("form[action='/me/second-factor/off']")).to_be_visible()
        assert fits_the_screen(page)
    finally:
        context.close()
