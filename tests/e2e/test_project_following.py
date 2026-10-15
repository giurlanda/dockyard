"""Following a running Project in a browser: its CPU and RAM, its live log, and its console
(with the in-memory Runtime)."""

import time
from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, PHONE, PUBLIC_REPOSITORY, OpenAs, fits_the_screen
from .test_project_running import connect_claude_account


def running_project(page: Page, running: str = "Running", **sign_in: str) -> str:
    """A new Project, once its Remote Control Server runs; its page is open."""
    connect_claude_account(page, **sign_in)
    page.goto("/projects/new")
    page.locator("#repository").fill(PUBLIC_REPOSITORY)
    page.locator("#name").fill("Hello")
    page.locator("form.fields button.primary").click()
    expect(page.get_by_test_id("project-status")).to_have_text(running)
    return page.url.rsplit("/", 1)[-1]


def eventually(condition: Callable[[], bool], timeout: float = 5) -> bool:
    """For what the browser makes Dockyard do in the background."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def test_the_project_page_shows_the_cpu_and_ram_it_uses_as_they_change(
    open_as: OpenAs, runtime: FakeRuntime
) -> None:
    page = open_as(ADMINISTRATOR)
    project = running_project(page)

    runtime.use(project, cpu=37.5, memory=512 * 1024**2)
    expect(page.get_by_test_id("cpu")).to_have_text("37.5%")
    expect(page.get_by_test_id("memory")).to_have_text("512 MiB / 4 GiB")
    expect(page.get_by_text("this Project may use 2.")).to_be_visible()

    runtime.use(project, cpu=120, memory=3 * 1024**3)
    expect(page.get_by_test_id("cpu")).to_have_text("120%")
    expect(page.get_by_test_id("memory")).to_have_text("3 GiB / 4 GiB")


def test_the_log_follows_the_server_until_it_stops_and_lets_go_when_the_page_closes(
    open_as: OpenAs, runtime: FakeRuntime
) -> None:
    page = open_as(ADMINISTRATOR)
    project = running_project(page)
    page.get_by_role("link", name="Log", exact=True).click()

    expect(page.locator("[data-terminal]")).to_contain_text("Remote Control ready")
    assert eventually(lambda: runtime.watching(project) == 1)
    page.close()
    assert eventually(lambda: runtime.watching(project) == 0)

    log = open_as(ADMINISTRATOR)
    log.goto(f"/projects/{project}/logs")
    expect(log.locator("[data-terminal]")).to_contain_text("Remote Control ready")
    runtime.stop_project(project)
    expect(log.get_by_text("The Remote Control Server stopped or restarted")).to_be_visible()


def test_a_user_answers_the_trust_prompt_in_the_console(
    open_as: OpenAs, runtime: FakeRuntime
) -> None:
    page = open_as(ADMINISTRATOR)
    project = running_project(page)

    runtime.lose_trust(project)
    page.reload()
    expect(page.get_by_test_id("project-status")).to_have_text("Waiting for your answer")
    page.get_by_role("link", name="Open the console").first.click()

    expect(
        page.get_by_text("Ctrl+C in the console stops the Remote Control Server")
    ).to_be_visible()
    terminal = page.locator("[data-terminal]")
    expect(terminal).to_contain_text("Quick safety check")
    page.get_by_label("Answer", exact=True).fill("1")
    page.get_by_role("button", name="Send").click()
    expect(terminal).to_contain_text("Remote Control ready")

    page.get_by_role("link", name="Back to the Project").click()
    expect(page.get_by_test_id("project-status")).to_have_text("Running")


def test_the_console_takes_keys_typed_in_the_terminal(
    open_as: OpenAs, runtime: FakeRuntime
) -> None:
    page = open_as(ADMINISTRATOR)
    project = running_project(page)
    runtime.lose_trust(project)
    page.goto(f"/projects/{project}/console")
    terminal = page.locator("[data-terminal]")
    expect(terminal).to_contain_text("Quick safety check")

    terminal.click()
    page.keyboard.press("Enter")  # the first option, "Yes, I trust this folder"

    expect(terminal).to_contain_text("Remote Control ready")


@pytest.mark.parametrize(
    ("language", "labels", "back"),
    [
        ("English", {}, "Back to the Project"),
        (
            "Italiano",
            {"connect": "Collega", "code": "Codice", "send": "Invia", "running": "In esecuzione"},
            "Torna al Progetto",
        ),
    ],
)
def test_on_a_phone_the_log_and_console_fit_the_screen(
    open_as: OpenAs, language: str, labels: dict[str, str], back: str
) -> None:
    page = open_as(ADMINISTRATOR, viewport=PHONE)
    page.goto("/")
    page.get_by_role("button", name=language).click()
    project = running_project(page, **labels)
    assert fits_the_screen(page)

    for stream in ("logs", "console"):
        page.goto(f"/projects/{project}/{stream}")
        expect(page.locator("[data-terminal]")).to_contain_text("Remote Control ready")
        expect(page.get_by_role("link", name=back)).to_be_visible()
        assert fits_the_screen(page)
