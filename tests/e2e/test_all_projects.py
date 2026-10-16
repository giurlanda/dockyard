"""An Administrator looks after every Project on the Instance, in a browser (with the in-memory
Runtime)."""

import pytest
from playwright.sync_api import Page, expect

from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, PHONE, OpenAs, fits_the_screen
from .test_project_running import connect_claude_account, create_project

PERSON = "a.user@dockyard.example.com"


def a_user_with_a_running_project(open_as: OpenAs, email: str, name: str) -> str:
    """The Administrator invites the person, who creates a Project; its id once it runs."""
    administrator = open_as(ADMINISTRATOR)
    administrator.goto("/users")
    administrator.get_by_label("Email").fill(email)
    administrator.get_by_role("button", name="Invite").click()
    person = open_as(email)
    connect_claude_account(person)
    create_project(person, name)
    expect(person.get_by_test_id("project-status")).to_have_text("Running")
    return person.url.rsplit("/", 1)[-1]


def all_projects(page: Page, link: str = "All Projects") -> None:
    page.goto("/")
    page.get_by_role("link", name=link).click()


def test_an_administrator_sees_another_users_project_then_stops_and_deletes_it(
    open_as: OpenAs, runtime: FakeRuntime
) -> None:
    project = a_user_with_a_running_project(open_as, PERSON, "Theirs")
    runtime.use(project, cpu=25.0, memory=512 * 1024**2)
    administrator = open_as(ADMINISTRATOR)

    all_projects(administrator)

    row = administrator.get_by_role("listitem").filter(has_text="Theirs")
    expect(row.get_by_test_id("project-user")).to_have_text(PERSON)
    expect(row.get_by_test_id("project-status")).to_have_text("Running")
    expect(row.get_by_text("CPUs up to 2, RAM up to 4 GiB")).to_be_visible()
    expect(row.get_by_test_id("cpu")).to_have_text("25%")
    expect(row.get_by_test_id("memory")).to_have_text("512 MiB / 4 GiB")
    expect(row.get_by_role("link", name="Theirs")).to_have_count(0)  # no way into their Terminal

    row.get_by_role("button", name="Stop").click()
    expect(row.get_by_test_id("project-status")).to_have_text("Stopped")
    expect(row.get_by_role("button", name="Stop")).to_have_count(0)

    row.get_by_label("Also delete its code and its home folder").check()
    row.get_by_role("button", name="Delete Project").click()
    expect(administrator.get_by_text("There are no Projects on this Instance yet.")).to_be_visible()
    person = open_as(PERSON)
    person.goto("/")
    expect(person.get_by_text("You have no Projects yet.")).to_be_visible()


@pytest.mark.parametrize(
    ("language", "link", "status"),
    [
        ("English", "All Projects", "Running"),
        ("Italiano", "Tutti i Progetti", "In esecuzione"),
    ],
)
def test_on_a_phone_the_all_projects_page_fits_the_screen(
    open_as: OpenAs, language: str, link: str, status: str
) -> None:
    long_email = "someone.with.a.rather.long.address@a-long-organisation-name.example.com"
    a_user_with_a_running_project(open_as, long_email, "A Project with a rather long name")
    administrator = open_as(ADMINISTRATOR, viewport=PHONE)
    administrator.goto("/")
    administrator.get_by_role("button", name=language).click()

    all_projects(administrator, link)

    expect(administrator.get_by_test_id("project-status")).to_have_text(status)
    expect(administrator.get_by_test_id("project-user")).to_have_text(long_email)
    assert fits_the_screen(administrator)
