"""Creating, following and deleting a public Project, in a browser (with the in-memory Runtime)."""

import pytest
from playwright.sync_api import Page, expect

from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, PHONE, PUBLIC_REPOSITORY, OpenAs, fits_the_screen

MISSING_REPOSITORY = "https://github.com/octocat/no-such-repository"


def fill_in_new_project(
    page: Page,
    repository: str,
    name: str,
    *,
    labels: tuple[str, str, str, str] = (
        "New Project",
        "Repository address",
        "Name",
        "Create Project",
    ),
) -> None:
    new, address, name_label, create = labels
    page.goto("/")
    page.get_by_role("link", name=new).click()
    page.get_by_label(address).fill(repository)
    page.get_by_label(name_label, exact=True).fill(name)
    page.get_by_role("button", name=create).click()


def test_a_user_creates_a_project_follows_its_clone_and_finds_it_on_the_dashboard(
    open_as: OpenAs, runtime: FakeRuntime
) -> None:
    page = open_as(ADMINISTRATOR)

    with runtime.clones_paused():
        fill_in_new_project(page, PUBLIC_REPOSITORY, "Hello World")
        expect(page.get_by_role("heading", name="Hello World")).to_be_visible()
        expect(page.get_by_test_id("project-status")).to_have_text("Cloning")
        expect(page.get_by_role("log", name="Clone progress")).to_contain_text("Cloning into")

    expect(page.get_by_test_id("project-status")).to_have_text("Waiting for Claude Account")
    page.get_by_role("link", name="Back to the dashboard").click()
    project = page.get_by_role("listitem").filter(has_text="Hello World")
    expect(project.get_by_test_id("project-status")).to_have_text("Waiting for Claude Account")
    expect(project).to_contain_text(PUBLIC_REPOSITORY)


def test_a_refused_address_is_explained_and_can_be_corrected(open_as: OpenAs) -> None:
    page = open_as(ADMINISTRATOR)

    fill_in_new_project(page, f"{PUBLIC_REPOSITORY}?tab=readme", "Hello")

    expect(page.get_by_role("alert")).to_contain_text("Remove everything from the ? onwards")
    page.get_by_label("Repository address").fill(PUBLIC_REPOSITORY)
    page.get_by_role("button", name="Create Project").click()
    expect(page.get_by_role("heading", name="Hello")).to_be_visible()


def test_a_failed_clone_shows_gits_error_and_the_user_tries_again(open_as: OpenAs) -> None:
    page = open_as(ADMINISTRATOR)

    fill_in_new_project(page, MISSING_REPOSITORY, "Oops")

    expect(page.get_by_role("heading", name="The clone failed")).to_be_visible()
    expect(page.get_by_text("fatal: could not read Username")).to_be_visible()
    page.get_by_role("link", name="Try again").click()
    expect(page.get_by_label("Repository address")).to_have_value(MISSING_REPOSITORY)
    expect(page.get_by_label("Name", exact=True)).to_have_value("Oops")


@pytest.mark.parametrize(
    ("delete_code", "code_after"), [(False, "stopped"), (True, "missing")], ids=["keep", "delete"]
)
def test_a_user_deletes_a_project_choosing_whether_to_delete_its_code(
    open_as: OpenAs, runtime: FakeRuntime, delete_code: bool, code_after: str
) -> None:
    page = open_as(ADMINISTRATOR)
    fill_in_new_project(page, PUBLIC_REPOSITORY, "Short-lived")
    expect(page.get_by_test_id("project-status")).to_have_text("Waiting for Claude Account")
    project = page.url.rsplit("/", 1)[-1]

    if delete_code:
        page.get_by_label("Also delete its code").check()
    page.get_by_role("button", name="Delete Project").click()

    expect(page.get_by_text("You have no Projects yet.")).to_be_visible()
    assert runtime.project_status(project) == code_after


@pytest.mark.parametrize(
    ("language", "labels", "cloning", "failed"),
    [
        (
            "English",
            ("New Project", "Repository address", "Name", "Create Project"),
            "Cloning",
            "The clone failed",
        ),
        (
            "Italiano",
            ("Nuovo Progetto", "Indirizzo del repository", "Nome", "Crea il Progetto"),
            "Clonazione in corso",
            "La clonazione non è riuscita",
        ),
    ],
)
def test_on_a_phone_every_project_page_fits_the_screen(
    open_as: OpenAs,
    runtime: FakeRuntime,
    language: str,
    labels: tuple[str, str, str, str],
    cloning: str,
    failed: str,
) -> None:
    page = open_as(ADMINISTRATOR, viewport=PHONE)
    page.goto("/")
    page.get_by_role("button", name=language).click()
    expect(page.get_by_role("button", name=language)).to_have_attribute("aria-pressed", "true")

    with runtime.clones_paused():
        fill_in_new_project(
            page, PUBLIC_REPOSITORY, "A Project with a rather long name", labels=labels
        )
        expect(page.get_by_test_id("project-status")).to_have_text(cloning)
        assert fits_the_screen(page)
    page.goto("/")
    assert fits_the_screen(page)

    page.get_by_role("link", name=labels[0]).click()
    assert fits_the_screen(page)
    fill_in_new_project(page, MISSING_REPOSITORY + "-" * 80, "Oops", labels=labels)
    expect(page.get_by_role("heading", name=failed)).to_be_visible()
    assert fits_the_screen(page)
