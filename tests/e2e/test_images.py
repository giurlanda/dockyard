"""Images in a browser (with the in-memory Runtime): an Administrator defines one, follows its build
until it is ready, then a User picks it for a new Project."""

import pytest
from playwright.sync_api import Page, expect

from .conftest import ADMINISTRATOR, PHONE, PUBLIC_REPOSITORY, OpenAs, fits_the_screen

INSTRUCTIONS = "RUN echo rust-toolchain > /etc/rust"


def create_image(page: Page, label: str = "Rust", instructions: str = INSTRUCTIONS) -> None:
    page.goto("/")
    page.get_by_role("link", name="Images").click()
    page.get_by_role("link", name="New Image").click()
    page.get_by_label("Label").fill(label)
    page.get_by_label("Description").fill("The Rust toolchain, for Rust repositories.")
    page.get_by_label("Build instructions").fill(instructions)
    page.get_by_role("button", name="Build the Image").click()


def test_an_administrator_builds_an_image_that_a_user_then_picks_for_a_project(
    open_as: OpenAs,
) -> None:
    page = open_as(ADMINISTRATOR)

    create_image(page)

    expect(page.get_by_role("heading", name="Rust")).to_be_visible()
    expect(page.get_by_test_id("image-status")).to_have_text("Ready")
    expect(page.get_by_role("log")).to_contain_text("USER root")
    page.goto("/projects/new")
    images = page.get_by_role("group", name="Image")
    expect(images.get_by_role("radio", name="Base Image")).to_be_checked()
    expect(images).to_contain_text("The Rust toolchain, for Rust repositories.")
    page.get_by_label("Repository address").fill(PUBLIC_REPOSITORY)
    page.get_by_label("Name", exact=True).fill("Rusty")
    images.get_by_role("radio", name="Rust").check()
    page.get_by_role("button", name="Create Project").click()

    expect(page.get_by_role("heading", name="Rusty")).to_be_visible()
    expect(page.get_by_text("Image: Rust")).to_be_visible()


def test_a_broken_image_is_never_offered_and_its_page_says_why(open_as: OpenAs) -> None:
    page = open_as(ADMINISTRATOR)

    create_image(page, label="Broken", instructions="RUN false")

    expect(page.get_by_test_id("image-status")).to_have_text("Failed")
    expect(page.get_by_text("The build failed")).to_be_visible()
    expect(page.get_by_role("log")).to_contain_text("returned a non-zero code: 1")
    page.goto("/projects/new")
    expect(page.get_by_role("radio", name="Broken")).to_have_count(0)


def test_an_image_in_use_cannot_be_deleted_until_its_projects_are(open_as: OpenAs) -> None:
    page = open_as(ADMINISTRATOR)
    create_image(page)
    image_page = page.url
    page.goto("/projects/new")
    page.get_by_label("Repository address").fill(PUBLIC_REPOSITORY)
    page.get_by_label("Name", exact=True).fill("Rusty")
    page.get_by_role("radio", name="Rust").check()
    page.get_by_role("button", name="Create Project").click()
    expect(page.get_by_role("heading", name="Rusty")).to_be_visible()
    project_page = page.url

    page.goto(image_page)

    expect(page.get_by_role("list", name="Projects using this Image")).to_contain_text("Rusty")
    expect(page.get_by_role("button", name="Delete Image")).to_have_count(0)
    page.goto(project_page)
    page.get_by_role("button", name="Delete Project").click()
    page.goto(image_page)
    page.get_by_role("button", name="Delete Image").click()
    expect(page.get_by_role("heading", name="Images")).to_be_visible()
    expect(page.get_by_role("link", name="Rust", exact=True)).to_have_count(0)


@pytest.mark.parametrize(
    ("language", "heading", "new_image"),
    [("English", "Images", "New Image"), ("Italiano", "Immagini", "Nuova Immagine")],
)
def test_on_a_phone_the_image_pages_fit_the_screen_in_every_language(
    open_as: OpenAs, language: str, heading: str, new_image: str
) -> None:
    page = open_as(ADMINISTRATOR, viewport=PHONE)
    create_image(page, instructions="RUN apt-get update && apt-get install -y " + "x" * 120)
    image_page = page.url
    page.get_by_role("button", name=language).click()
    expect(page.get_by_role("button", name=language)).to_have_attribute("aria-pressed", "true")
    assert fits_the_screen(page)

    for path in ("/images", "/images/new", "/projects/new"):
        page.goto(path)
        assert fits_the_screen(page), path
    page.goto("/images")
    expect(page.get_by_role("heading", name=heading)).to_be_visible()
    expect(page.get_by_role("link", name=new_image)).to_be_visible()
    page.goto(image_page)
    assert fits_the_screen(page)
