"""Running a Project's Remote Control Server, in a browser (with the in-memory Runtime)."""

import pytest
from playwright.sync_api import Page, expect

from .conftest import ADMINISTRATOR, PHONE, PUBLIC_REPOSITORY, OpenAs, fits_the_screen

CODE = "the-code-from-claude-ai"


def connect_claude_account(
    page: Page, connect: str = "Connect", code: str = "Code", send: str = "Send"
) -> None:
    page.goto("/")
    page.get_by_role("button", name=connect, exact=True).click()
    expect(page.locator("[data-terminal]")).to_contain_text("oauth/authorize")
    page.get_by_label(code, exact=True).fill(CODE)
    page.get_by_role("button", name=send).click()
    expect(page.locator("[data-outcome=connected]")).to_be_visible()


def create_project(page: Page, name: str, mode: str | None = None) -> None:
    page.goto("/projects/new")
    page.get_by_label("Repository address").fill(PUBLIC_REPOSITORY)
    page.get_by_label("Name", exact=True).fill(name)
    if mode:
        page.get_by_label(mode).check()
    page.get_by_role("button", name="Create Project").click()


def test_the_bypass_permissions_warning_appears_only_when_that_mode_is_chosen(
    open_as: OpenAs,
) -> None:
    page = open_as(ADMINISTRATOR)
    page.goto("/projects/new")
    warning = page.get_by_text("Only for repositories you fully trust")
    expect(page.get_by_label("Ask first")).to_be_checked()
    expect(warning).to_be_hidden()

    page.get_by_label("Bypass permissions").check()
    expect(warning).to_be_visible()

    page.get_by_label("Accept edits").check()
    expect(warning).to_be_hidden()


def test_a_waiting_project_starts_once_the_claude_account_is_connected(open_as: OpenAs) -> None:
    page = open_as(ADMINISTRATOR)

    create_project(page, "Hello World", "Accept edits")

    expect(page.get_by_test_id("project-status")).to_have_text("Waiting for Claude Account")
    expect(page.get_by_text("Accept edits")).to_be_visible()
    expect(
        page.get_by_text("The first Session works directly in the main checkout")
    ).to_be_visible()
    waiting = open_as(ADMINISTRATOR)  # the same User, in another tab
    waiting.goto(page.url)
    connect_claude_account(page)
    expect(waiting.get_by_test_id("project-status")).to_have_text("Running")
    page = waiting
    expect(page.get_by_text("this Project appears there as “Hello World”")).to_be_visible()


def test_a_user_stops_and_starts_a_project(open_as: OpenAs) -> None:
    page = open_as(ADMINISTRATOR)
    connect_claude_account(page)
    create_project(page, "Hello")
    expect(page.get_by_test_id("project-status")).to_have_text("Running")

    page.get_by_role("button", name="Stop").click()
    expect(page.get_by_test_id("project-status")).to_have_text("Stopped")
    page.get_by_role("button", name="Start").click()
    expect(page.get_by_test_id("project-status")).to_have_text("Running")
    page.get_by_role("button", name="Restart").click()
    expect(page.get_by_test_id("project-status")).to_have_text("Running")

    page.get_by_role("link", name="Back to the dashboard").click()
    project = page.get_by_role("listitem").filter(has_text="Hello")
    expect(project.get_by_test_id("project-status")).to_have_text("Running")


@pytest.mark.parametrize(
    ("language", "connect", "code", "send", "running", "stop", "stopped", "bypass"),
    [
        ("English", "Connect", "Code", "Send", "Running", "Stop", "Stopped", "Bypass permissions"),
        (
            "Italiano",
            "Collega",
            "Codice",
            "Invia",
            "In esecuzione",
            "Ferma",
            "Fermo",
            "Ignora i permessi",
        ),
    ],
)
def test_on_a_phone_a_running_project_and_its_controls_fit_the_screen(
    open_as: OpenAs,
    language: str,
    connect: str,
    code: str,
    send: str,
    running: str,
    stop: str,
    stopped: str,
    bypass: str,
) -> None:
    page = open_as(ADMINISTRATOR, viewport=PHONE)
    page.goto("/")
    page.get_by_role("button", name=language).click()
    expect(page.get_by_role("button", name=language)).to_have_attribute("aria-pressed", "true")
    connect_claude_account(page, connect, code, send)

    page.goto("/projects/new")
    page.get_by_label(bypass).check()
    assert fits_the_screen(page)
    page.locator("#repository").fill(PUBLIC_REPOSITORY)
    page.locator("#name").fill("A Project with a rather long name for a phone")
    page.locator("form.fields button.primary").click()

    expect(page.get_by_test_id("project-status")).to_have_text(running)
    assert fits_the_screen(page)
    page.get_by_role("button", name=stop).click()
    expect(page.get_by_test_id("project-status")).to_have_text(stopped)
    assert fits_the_screen(page)
    page.goto("/")
    assert fits_the_screen(page)
