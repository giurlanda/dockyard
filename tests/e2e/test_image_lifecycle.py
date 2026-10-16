"""The life of Images in a browser (with the in-memory Runtime): a User changes the Image of a
stopped Project and sees when a newer build is there; an Administrator sees failed builds."""

import pytest
from playwright.sync_api import Page, expect

from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, PHONE, OpenAs, fits_the_screen
from .test_images import create_image
from .test_project_running import connect_claude_account, create_project


def running_project(page: Page, name: str = "Hello") -> str:
    """A new running Project, by the address of its page."""
    connect_claude_account(page)
    create_project(page, name)
    expect(page.get_by_test_id("project-status")).to_have_text("Running")
    return page.url


def test_a_user_changes_the_image_of_a_stopped_project(open_as: OpenAs) -> None:
    page = open_as(ADMINISTRATOR)
    create_image(page)
    project = running_project(page)
    page.goto(project)
    expect(page.get_by_text("Stop the Project to change its Image")).to_be_visible()

    page.get_by_role("button", name="Stop").click()
    images = page.get_by_role("group", name="Image")
    expect(images.get_by_role("radio", name="Base Image")).to_be_checked()
    images.get_by_role("radio", name="Rust").check()
    page.get_by_role("button", name="Change Image").click()

    expect(page.get_by_text("Image: Rust")).to_be_visible()
    page.get_by_role("button", name="Start").click()
    expect(page.get_by_test_id("project-status")).to_have_text("Running")


def test_a_project_on_an_older_build_shows_update_available_until_it_restarts(
    open_as: OpenAs, runtime: FakeRuntime
) -> None:
    page = open_as(ADMINISTRATOR)
    project = running_project(page)

    runtime.rebuild_base()

    page.goto("/")
    expect(page.get_by_test_id("project-update")).to_have_text("Update available")
    page.goto(project)
    expect(page.get_by_text("Restart it to move to the newer build")).to_be_visible()
    page.get_by_role("button", name="Restart").click()
    expect(page.get_by_test_id("project-status")).to_have_text("Running")
    expect(page.get_by_test_id("project-update")).to_have_count(0)


def test_a_failed_build_is_highlighted_on_the_administrators_dashboard(
    open_as: OpenAs, runtime: FakeRuntime
) -> None:
    page = open_as(ADMINISTRATOR)
    with runtime.builds_failing():
        create_image(page)
        expect(page.get_by_test_id("image-status")).to_have_text("Failed")

    page.goto("/")
    failed = page.get_by_role("list", name="Images that failed to build")
    failed.get_by_role("link", name="Rust").click()
    page.get_by_role("button", name="Build again").click()
    expect(page.get_by_test_id("image-status")).to_have_text("Ready")
    page.goto("/")
    expect(page.get_by_role("list", name="Images that failed to build")).to_have_count(0)


@pytest.mark.parametrize(
    ("language", "change", "update"),
    [
        ("English", "Change Image", "Update available"),
        ("Italiano", "Cambia Immagine", "Aggiornamento disponibile"),
    ],
)
def test_on_a_phone_the_image_lifecycle_fits_the_screen_in_every_language(
    open_as: OpenAs, runtime: FakeRuntime, language: str, change: str, update: str
) -> None:
    page = open_as(ADMINISTRATOR, viewport=PHONE)
    with runtime.builds_failing():
        create_image(page, label="Broken " + "x" * 50)
    create_image(page)
    project = running_project(page, name="A Project with a rather long name " + "y" * 20)
    runtime.rebuild_base()
    page.goto(project)
    page.get_by_role("button", name=language).click()
    expect(page.get_by_role("button", name=language)).to_have_attribute("aria-pressed", "true")

    expect(page.get_by_test_id("project-update")).to_have_text(update)
    assert fits_the_screen(page)
    page.goto("/")
    assert fits_the_screen(page)
    page.goto(project)
    page.locator("form[action$='/stop'] button").click()
    expect(page.get_by_role("button", name=change)).to_be_visible()
    assert fits_the_screen(page)
