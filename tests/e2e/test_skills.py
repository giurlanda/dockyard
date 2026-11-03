"""Skills in a browser (with the in-memory Runtime): an Administrator uploads one, a User reads it,
picks it for a Project, then removes it from the running Project; on a phone, in every language."""

import pytest
from playwright.sync_api import Page, expect

from .conftest import ADMINISTRATOR, PHONE, PUBLIC_REPOSITORY, OpenAs, fits_the_screen
from .test_project_running import connect_claude_account
from .test_users import INVITED, invite

SKILL_FILE = b"---\ndescription: Review a diff against the team's rules.\n---\nRead the diff.\n"


def upload_skill(page: Page, name: str = "review") -> None:
    page.goto("/")
    page.get_by_role("link", name="Skills").click()
    page.get_by_label("Markdown file or .zip").set_input_files(
        {"name": f"{name}.md", "mimeType": "text/markdown", "buffer": SKILL_FILE}
    )
    page.get_by_role("button", name="Upload").click()
    expect(page.get_by_role("heading", name=name, exact=True)).to_be_visible()


def test_a_user_picks_an_uploaded_skill_for_a_project_then_removes_it(open_as: OpenAs) -> None:
    administrator = open_as(ADMINISTRATOR)
    upload_skill(administrator)
    invite(administrator, INVITED)
    user = open_as(INVITED)

    user.goto("/projects/new")
    with user.context.expect_page() as reading:
        user.get_by_role("link", name="Read it").click()
    skill_page = reading.value
    expect(skill_page.get_by_role("heading", name="review")).to_be_visible()
    expect(skill_page.get_by_text("Read the diff.")).to_be_visible()
    expect(skill_page.get_by_role("button", name="Delete Skill")).to_have_count(0)
    skill_page.close()
    connect_claude_account(user)
    user.goto("/projects/new")
    user.get_by_label("Repository address").fill(PUBLIC_REPOSITORY)
    user.get_by_label("Name", exact=True).fill("Reviewed")
    user.get_by_role("checkbox", name="review").check()
    user.get_by_role("button", name="Create Project").click()

    expect(user.get_by_test_id("project-status")).to_have_text("Running")
    expect(user.get_by_role("link", name="review")).to_be_visible()
    user.get_by_role("group", name="Skills").get_by_role("checkbox", name="review").uncheck()
    user.get_by_role("button", name="Change Skills").click()
    expect(user.get_by_text("Skills: None")).to_be_visible()
    expect(user.get_by_text("Restart it to pick them up")).to_be_visible()


@pytest.mark.parametrize(
    ("language", "skills", "precedence", "change"),
    [
        ("English", "Skills", "wins over a skill with the same name", "Change Skills"),
        ("Italiano", "Skill", "prevale su una skill con lo stesso nome", "Cambia Skill"),
    ],
)
def test_on_a_phone_the_skill_pages_fit_the_screen_in_every_language(
    open_as: OpenAs, language: str, skills: str, precedence: str, change: str
) -> None:
    page = open_as(ADMINISTRATOR, viewport=PHONE)
    name = "a-skill-with-a-rather-long-name-" + "x" * 30
    upload_skill(page, name=name)
    skill_page = page.url
    page.get_by_role("button", name=language).click()
    expect(page.get_by_role("button", name=language)).to_have_attribute("aria-pressed", "true")
    assert fits_the_screen(page)

    page.goto("/skills")
    expect(page.get_by_role("heading", name=skills, exact=True)).to_be_visible()
    expect(page.get_by_text(precedence)).to_be_visible()
    assert fits_the_screen(page)
    page.goto("/projects/new")
    page.get_by_role("group", name=skills, exact=True).get_by_role("checkbox").check()
    assert fits_the_screen(page)
    page.locator("#repository").fill(PUBLIC_REPOSITORY)
    page.locator("#name").fill("Hello")
    page.locator("form[action='/projects'] button.primary").click()
    expect(page.get_by_role("link", name=name)).to_be_visible()
    expect(page.get_by_role("button", name=change)).to_be_visible()
    assert fits_the_screen(page)
    page.goto(skill_page)
    assert fits_the_screen(page)
