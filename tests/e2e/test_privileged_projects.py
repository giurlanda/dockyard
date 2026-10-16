"""Privileged Projects in a browser (with the in-memory Runtime): an Administrator grants the
permission, then the option appears, with its warning, on the new Project form."""

from urllib.parse import quote

import pytest
from playwright.sync_api import Page, expect

from .conftest import ADMINISTRATOR, PHONE, PUBLIC_REPOSITORY, OpenAs, fits_the_screen


def grant_to_myself(page: Page) -> None:
    page.goto(f"/users/{quote(ADMINISTRATOR)}")
    page.get_by_role("button", name="Grant the permission").click()
    expect(page.get_by_text("They may create Privileged Projects.")).to_be_visible()


def test_the_option_appears_only_once_an_administrator_grants_the_permission(
    open_as: OpenAs,
) -> None:
    page = open_as(ADMINISTRATOR)
    page.goto("/projects/new")
    expect(page.get_by_role("group", name="Privileged Project")).to_have_count(0)

    grant_to_myself(page)
    page.goto("/projects/new")

    privileged = page.get_by_role("group", name="Privileged Project")
    expect(privileged).to_contain_text("equals root on the host")
    expect(privileged).to_contain_text("code, Git Credentials and Claude Account")
    expect(privileged.get_by_role("checkbox")).not_to_be_checked()


def test_a_project_created_privileged_shows_it_and_its_warning(open_as: OpenAs) -> None:
    page = open_as(ADMINISTRATOR)
    grant_to_myself(page)
    page.goto("/projects/new")
    page.get_by_label("Repository address").fill(PUBLIC_REPOSITORY)
    page.get_by_label("Name", exact=True).fill("Docker in Docker")

    page.get_by_label("I understand that it equals root on the host").check()
    page.get_by_role("button", name="Create Project").click()

    expect(page.get_by_role("heading", name="Docker in Docker")).to_be_visible()
    expect(page.get_by_text("Privileged", exact=True)).to_be_visible()
    expect(page.get_by_text("A Privileged Project equals root on the host.")).to_be_visible()


def test_revoking_the_permission_hides_the_option_again(open_as: OpenAs) -> None:
    page = open_as(ADMINISTRATOR)
    grant_to_myself(page)

    page.get_by_role("button", name="Revoke the permission").click()

    expect(page.get_by_text("They cannot create Privileged Projects.")).to_be_visible()
    page.goto("/projects/new")
    expect(page.get_by_role("group", name="Privileged Project")).to_have_count(0)


@pytest.mark.parametrize(
    ("language", "heading", "option"),
    [
        ("English", "Privileged Projects", "Privileged Project"),
        ("Italiano", "Progetti privilegiati", "Progetto privilegiato"),
    ],
)
def test_on_a_phone_the_permission_and_the_option_fit_the_screen_in_every_language(
    open_as: OpenAs, language: str, heading: str, option: str
) -> None:
    page = open_as(ADMINISTRATOR, viewport=PHONE)
    grant_to_myself(page)
    page.get_by_role("button", name=language).click()
    expect(page.get_by_role("button", name=language)).to_have_attribute("aria-pressed", "true")

    expect(page.get_by_role("heading", name=heading)).to_be_visible()
    assert fits_the_screen(page)
    page.goto("/projects/new")
    expect(page.get_by_role("group", name=option)).to_be_visible()
    assert fits_the_screen(page)
