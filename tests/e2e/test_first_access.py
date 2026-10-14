"""First access to a fresh Instance, in a browser."""

import pytest
from playwright.sync_api import Page, expect

from .conftest import ADMINISTRATOR, PHONE, OpenAs

STRANGER = "someone.who.was.never.invited@another-organisation.example.com"


def fits_the_screen(page: Page) -> bool:
    return bool(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))


def test_the_first_administrator_lands_on_their_dashboard(open_as: OpenAs) -> None:
    page = open_as(ADMINISTRATOR)
    page.goto("/")

    expect(page.get_by_test_id("user-email")).to_have_text(ADMINISTRATOR)
    expect(page.get_by_test_id("user-role")).to_have_text("Administrator")
    expect(page.get_by_role("heading", name="Projects")).to_be_visible()


def test_a_verified_email_that_was_never_invited_gets_the_not_invited_page(
    open_as: OpenAs,
) -> None:
    page = open_as(STRANGER)
    page.goto("/")

    expect(page.get_by_role("heading", name="Not invited")).to_be_visible()
    expect(page.get_by_text(STRANGER)).to_be_visible()
    expect(page.get_by_role("heading", name="Projects")).to_have_count(0)


def test_the_administrator_switches_the_dashboard_to_italian_and_back(open_as: OpenAs) -> None:
    page = open_as(ADMINISTRATOR)
    page.goto("/")

    page.get_by_role("button", name="Italiano").click()
    expect(page.get_by_role("heading", name="Progetti")).to_be_visible()
    expect(page.get_by_test_id("user-role")).to_have_text("Amministratore")

    page.reload()
    expect(page.get_by_role("heading", name="Progetti")).to_be_visible()

    page.get_by_role("button", name="English").click()
    expect(page.get_by_role("heading", name="Projects")).to_be_visible()


@pytest.mark.parametrize("language", ["English", "Italiano"])
def test_the_dashboard_fits_a_phone_screen(open_as: OpenAs, language: str) -> None:
    page = open_as(ADMINISTRATOR, viewport=PHONE)
    page.goto("/")
    page.get_by_role("button", name=language).click()
    expect(page.get_by_role("button", name=language)).to_have_attribute("aria-pressed", "true")

    assert fits_the_screen(page)


def websocket_close_code(page: Page) -> int:
    code: int = page.evaluate(
        """() => new Promise((resolve) => {
            const socket = new WebSocket(location.origin.replace("http", "ws") + "/ws/anything");
            socket.onclose = (event) => resolve(event.code);
        })"""
    )
    return code


@pytest.mark.parametrize(
    ("email", "close_code"), [(None, 4401), (STRANGER, 4403)], ids=["no-identity", "not-invited"]
)
def test_a_refused_websocket_tells_the_browser_why(
    open_as: OpenAs, email: str | None, close_code: int
) -> None:
    page = open_as(email)
    page.goto("/")

    assert websocket_close_code(page) == close_code


def test_the_not_invited_page_fits_a_phone_screen(open_as: OpenAs) -> None:
    page = open_as(STRANGER, viewport=PHONE)
    page.goto("/")
    expect(page.get_by_role("heading", name="Not invited")).to_be_visible()

    assert fits_the_screen(page)
