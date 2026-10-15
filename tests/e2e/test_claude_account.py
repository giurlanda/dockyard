"""Connecting a Claude Account from the dashboard, in a browser (with the in-memory Runtime)."""

import pytest
from playwright.sync_api import Locator, Page, expect

from .conftest import ADMINISTRATOR, PHONE, OpenAs, fits_the_screen

CODE = "the-code-from-claude-ai"


def start_signing_in(
    page: Page, connect: str = "Connect", terminal: str = "Login terminal"
) -> Locator:
    page.goto("/")
    page.get_by_role("button", name=connect, exact=True).click()
    login_terminal = page.get_by_role("region", name=terminal)
    expect(login_terminal).to_contain_text("oauth/authorize")
    return login_terminal


def test_a_user_connects_their_claude_account_by_pasting_the_code_in_the_terminal(
    open_as: OpenAs,
) -> None:
    page = open_as(ADMINISTRATOR)
    page.goto("/")
    expect(page.get_by_test_id("claude-account-status")).to_have_text("Not connected")
    expect(page.get_by_text("Do not use an API key or a long-lived token")).to_be_visible()

    terminal = start_signing_in(page)
    terminal.click()
    page.keyboard.type(CODE)
    page.keyboard.press("Enter")

    expect(terminal).to_contain_text("Login successful.")
    expect(page.get_by_text("Your Claude Account is connected.")).to_be_visible()
    page.get_by_role("link", name="Back to the dashboard").click()
    expect(page.get_by_test_id("claude-account-status")).to_have_text("Connected")
    expect(page.get_by_role("button", name="Reconnect")).to_be_visible()


@pytest.mark.parametrize(
    ("language", "connect", "terminal", "code", "send", "connected"),
    [
        (
            "English",
            "Connect",
            "Login terminal",
            "Code",
            "Send",
            "Your Claude Account is connected.",
        ),
        (
            "Italiano",
            "Collega",
            "Terminale di accesso",
            "Codice",
            "Invia",
            "Il tuo Account Claude è collegato.",
        ),
    ],
)
def test_on_a_phone_the_code_goes_in_a_field_and_every_page_fits_the_screen(
    open_as: OpenAs,
    language: str,
    connect: str,
    terminal: str,
    code: str,
    send: str,
    connected: str,
) -> None:
    page = open_as(ADMINISTRATOR, viewport=PHONE)
    page.goto("/")
    page.get_by_role("button", name=language).click()
    expect(page.get_by_role("button", name=language)).to_have_attribute("aria-pressed", "true")
    assert fits_the_screen(page)

    start_signing_in(page, connect, terminal)
    assert fits_the_screen(page)
    page.get_by_label(code, exact=True).fill(CODE)
    page.get_by_role("button", name=send).click()

    expect(page.get_by_text(connected)).to_be_visible()
    assert fits_the_screen(page)


def test_a_sign_in_without_a_code_says_the_account_is_not_connected(open_as: OpenAs) -> None:
    page = open_as(ADMINISTRATOR)
    terminal = start_signing_in(page)

    terminal.click()
    page.keyboard.press("Enter")

    expect(
        page.get_by_text("The sign-in ended without connecting your Claude Account")
    ).to_be_visible()


def test_a_user_cancels_a_sign_in_and_returns_to_the_dashboard(open_as: OpenAs) -> None:
    page = open_as(ADMINISTRATOR)
    start_signing_in(page)

    page.get_by_role("button", name="Cancel the sign-in").click()

    expect(page.get_by_test_id("claude-account-status")).to_have_text("Not connected")
    expect(page.get_by_role("button", name="Connect", exact=True)).to_be_visible()


def test_leaving_the_login_page_keeps_the_sign_in_waiting(open_as: OpenAs) -> None:
    page = open_as(ADMINISTRATOR)
    start_signing_in(page)

    page.get_by_role("link", name="Back to the dashboard").click()
    page.get_by_role("link", name="Continue signing in").click()

    expect(page.get_by_role("region", name="Login terminal")).to_contain_text("oauth/authorize")
