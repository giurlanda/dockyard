"""A Terminal in a running Project, in a browser (with the in-memory Runtime)."""

import re

import pytest
from playwright.sync_api import Page, expect

from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, PHONE, OpenAs, fits_the_screen
from .test_project_following import eventually, running_project


def shell_ready(page: Page) -> None:
    expect(page.locator("[data-terminal]")).to_contain_text("$")


def printed_by_pwd(project: str) -> re.Pattern[str]:
    """`pwd` typed after the prompt, and what it printed: the Project's path."""
    return re.compile(rf"\$ pwd\s*/workspace/{project}")


def test_a_user_opens_a_terminal_from_the_project_and_types_in_it(
    open_as: OpenAs, runtime: FakeRuntime
) -> None:
    page = open_as(ADMINISTRATOR)
    project = running_project(page)

    page.get_by_role("link", name="Terminal", exact=True).click()
    terminal = page.locator("[data-terminal]")
    shell_ready(page)
    terminal.click()
    page.keyboard.type("pwd")
    page.keyboard.press("Enter")

    expect(terminal).to_contain_text(printed_by_pwd(project))
    expect(page.get_by_text("Leaving this page closes the shell")).to_be_visible()


def test_leaving_the_terminal_ends_its_shell(open_as: OpenAs, runtime: FakeRuntime) -> None:
    page = open_as(ADMINISTRATOR)
    project = running_project(page)
    page.goto(f"/projects/{project}/terminal")
    shell_ready(page)
    assert eventually(lambda: runtime.terminal_processes(project) == 1)

    page.close()

    assert eventually(lambda: runtime.terminal_processes(project) == 0)


def test_once_the_shell_exits_the_page_says_so(open_as: OpenAs) -> None:
    page = open_as(ADMINISTRATOR)
    project = running_project(page)
    page.goto(f"/projects/{project}/terminal")
    shell_ready(page)

    page.get_by_label("Command", exact=True).fill("exit")
    page.get_by_role("button", name="Send").click()

    expect(page.get_by_text("The shell ended")).to_be_visible()


def test_the_terminal_of_a_stopped_project_says_to_start_it(open_as: OpenAs) -> None:
    page = open_as(ADMINISTRATOR)
    project = running_project(page)
    page.get_by_role("button", name="Stop").click()
    expect(page.get_by_test_id("project-status")).to_have_text("Stopped")

    page.goto(f"/projects/{project}/terminal")
    expect(page.get_by_text("This Project is not running: start it from its page.")).to_be_visible()
    page.get_by_role("button", name="Start").click()

    expect(page.get_by_test_id("project-status")).to_have_text("Running")


@pytest.mark.parametrize(
    ("language", "labels", "terminal", "command", "send"),
    [
        ("English", {}, "Terminal", "Command", "Send"),
        (
            "Italiano",
            {"connect": "Collega", "code": "Codice", "send": "Invia", "running": "In esecuzione"},
            "Terminale",
            "Comando",
            "Invia",
        ),
    ],
)
def test_on_a_phone_the_terminal_fits_the_screen_and_has_the_keys_a_phone_lacks(
    open_as: OpenAs,
    language: str,
    labels: dict[str, str],
    terminal: str,
    command: str,
    send: str,
) -> None:
    page = open_as(ADMINISTRATOR, viewport=PHONE)
    page.goto("/")
    page.get_by_role("button", name=language).click()
    project = running_project(page, **labels)
    page.get_by_role("link", name=terminal, exact=True).click()
    shell_ready(page)
    assert fits_the_screen(page)

    page.get_by_label(command, exact=True).fill("sleep 600")
    page.get_by_role("button", name=send).click()
    page.get_by_role("button", name="Ctrl+C").click()
    expect(page.locator("[data-terminal]")).to_contain_text("^C")
    page.get_by_label(command, exact=True).fill("pwd")
    page.get_by_role("button", name=send).click()

    expect(page.locator("[data-terminal]")).to_contain_text(printed_by_pwd(project))
    assert fits_the_screen(page)
