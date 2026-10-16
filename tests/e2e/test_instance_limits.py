"""Choosing a Project's CPU and RAM within the Instance limits, in a browser (with the in-memory
Runtime). A fresh Instance allows 2 CPUs and 4 GiB per Project, and 3 active Projects per User."""

import pytest
from playwright.sync_api import Page, expect

from .conftest import ADMINISTRATOR, PHONE, PUBLIC_REPOSITORY, OpenAs, fits_the_screen


def new_project(
    page: Page, name: str, *, cpus: str | None = None, memory: str | None = None
) -> None:
    page.goto("/projects/new")
    page.get_by_label("Repository address").fill(PUBLIC_REPOSITORY)
    page.get_by_label("Name", exact=True).fill(name)
    if cpus is not None:
        page.get_by_label("CPUs").fill(cpus)
    if memory is not None:
        page.get_by_label("RAM (GiB)").fill(memory)
    page.get_by_role("button", name="Create Project").click()


def test_a_user_chooses_the_cpu_and_ram_of_a_project_within_the_allowed_ranges(
    open_as: OpenAs,
) -> None:
    page = open_as(ADMINISTRATOR)
    page.goto("/projects/new")

    expect(page.get_by_label("CPUs")).to_have_value("2")
    expect(page.get_by_label("RAM (GiB)")).to_have_value("4")
    expect(page.get_by_text("Between 0.5 and 2; 2 unless you choose less.")).to_be_visible()
    expect(page.get_by_text("Between 1 and 4 GiB; 4 unless you choose less.")).to_be_visible()

    new_project(page, "Small", cpus="1.5", memory="2")

    expect(page.get_by_role("heading", name="Small")).to_be_visible()
    expect(page.get_by_text("CPUs up to 1.5, RAM up to 2 GiB")).to_be_visible()


def test_a_project_beyond_the_active_maximum_is_refused_until_one_is_stopped(
    open_as: OpenAs,
) -> None:
    page = open_as(ADMINISTRATOR)
    for name in ("First", "Second", "Third"):
        new_project(page, name)
        expect(page.get_by_role("heading", name=name)).to_be_visible()
    third = page.url

    new_project(page, "Fourth")

    expect(page.get_by_role("alert")).to_contain_text("stop one of them first")
    page.goto(third)
    page.get_by_role("button", name="Stop").click()
    expect(page.get_by_test_id("project-status")).to_have_text("Stopped")
    new_project(page, "Fourth")
    expect(page.get_by_role("heading", name="Fourth")).to_be_visible()


@pytest.mark.parametrize(
    ("language", "resources", "cpus", "memory"),
    [
        ("English", "Resources", "CPUs", "RAM (GiB)"),
        ("Italiano", "Risorse", "CPU", "RAM (GiB)"),
    ],
)
def test_on_a_phone_the_allowed_ranges_fit_the_screen_in_every_language(
    open_as: OpenAs, language: str, resources: str, cpus: str, memory: str
) -> None:
    page = open_as(ADMINISTRATOR, viewport=PHONE)
    page.goto("/")
    page.get_by_role("button", name=language).click()
    expect(page.get_by_role("button", name=language)).to_have_attribute("aria-pressed", "true")

    page.goto("/projects/new")

    expect(page.get_by_role("group", name=resources)).to_be_visible()
    expect(page.get_by_label(cpus, exact=True)).to_have_value("2")
    expect(page.get_by_label(memory)).to_have_value("4")
    assert fits_the_screen(page)
