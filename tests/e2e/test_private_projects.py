"""Creating a private Project with a Git Credential, in a browser (with the in-memory Runtime)."""

import pytest
from playwright.sync_api import Page, expect

from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, PHONE, OpenAs, fits_the_screen

PRIVATE_REPOSITORY = "https://github.com/acme/secret-sauce"
GITHUB_TOKEN = "github_pat_11AAAAAAA0the0private0repository0token"
GITLAB_TOKEN = "glpat-the0gitlab0token"


@pytest.fixture(autouse=True)
def private_repository(runtime: FakeRuntime) -> None:
    runtime.publish_repository(PRIVATE_REPOSITORY, token=GITHUB_TOKEN)


def add_git_credential(page: Page, label: str, provider: str, token: str) -> None:
    page.goto("/git-credentials/new")
    page.get_by_label("Label").fill(label)
    page.get_by_label(provider).check()
    page.get_by_label("Token").fill(token)
    page.get_by_role("button", name="Save Git Credential").click()


def test_a_user_creates_a_private_project_with_a_git_credential_of_its_provider(
    open_as: OpenAs,
) -> None:
    page = open_as(ADMINISTRATOR)
    add_git_credential(page, "Acme on GitHub", "GitHub", GITHUB_TOKEN)
    add_git_credential(page, "Acme on GitLab", "GitLab", GITLAB_TOKEN)

    page.goto("/projects/new")
    page.get_by_label("Repository address").fill(PRIVATE_REPOSITORY)
    page.get_by_label("Name", exact=True).fill("Secret sauce")
    page.get_by_label("Private").check()
    credential = page.get_by_label("Git Credential", exact=True)
    expect(credential.get_by_role("option")).to_have_text(["None", "Acme on GitHub"])
    credential.select_option(label="Acme on GitHub")
    page.get_by_role("button", name="Create Project").click()

    expect(page.get_by_role("heading", name="Secret sauce")).to_be_visible()
    expect(page.get_by_test_id("project-status")).to_have_text("Waiting for Claude Account")
    expect(page.get_by_role("link", name="Acme on GitHub")).to_be_visible()
    assert GITHUB_TOKEN not in page.content()


def test_a_git_credential_in_use_cannot_be_deleted_until_its_projects_are(
    open_as: OpenAs,
) -> None:
    page = open_as(ADMINISTRATOR)
    add_git_credential(page, "Acme", "GitHub", GITHUB_TOKEN)
    page.goto("/projects/new")
    page.get_by_label("Repository address").fill(PRIVATE_REPOSITORY)
    page.get_by_label("Name", exact=True).fill("Secret sauce")
    page.get_by_label("Private").check()
    page.get_by_label("Git Credential", exact=True).select_option(label="Acme")
    page.get_by_role("button", name="Create Project").click()
    expect(page.get_by_test_id("project-status")).to_have_text("Waiting for Claude Account")

    page.get_by_role("link", name="Acme", exact=True).click()
    used_by = page.get_by_role("list", name="Projects using this Git Credential")
    expect(used_by).to_have_text("Secret sauce")
    expect(page.get_by_role("button", name="Delete Git Credential")).to_have_count(0)

    page.goto("/")
    page.get_by_role("link", name="Secret sauce").click()
    page.get_by_role("button", name="Delete Project").click()
    page.get_by_role("link", name="Acme", exact=True).click()
    page.get_by_role("button", name="Delete Git Credential").click()
    expect(page.get_by_text("You have no Git Credentials yet.")).to_be_visible()


@pytest.mark.parametrize(
    ("language", "labels"),
    [
        ("English", ("Private", "Git Credential", "Create Project", "Delete these Projects")),
        ("Italiano", ("Privato", "Credenziale Git", "Crea il Progetto", "Elimina prima")),
    ],
)
def test_on_a_phone_the_private_project_pages_fit_the_screen(
    open_as: OpenAs, language: str, labels: tuple[str, str, str, str]
) -> None:
    private, credential, create, in_use = labels
    page = open_as(ADMINISTRATOR, viewport=PHONE)
    page.goto("/")
    page.get_by_role("button", name=language).click()
    expect(page.get_by_role("button", name=language)).to_have_attribute("aria-pressed", "true")
    add_git_credential_in(page, "A rather long label for a phone screen")

    page.goto("/projects/new")
    assert fits_the_screen(page)
    page.locator("#repository").fill(PRIVATE_REPOSITORY)
    page.locator("#name").fill("A Project with a rather long name")
    page.get_by_label(private).check()
    page.get_by_label(credential, exact=True).select_option(
        label="A rather long label for a phone screen"
    )
    page.get_by_role("button", name=create).click()
    expect(page.get_by_role("link", name="A rather long label for a phone screen")).to_be_visible()
    assert fits_the_screen(page)

    page.get_by_role("link", name="A rather long label for a phone screen").click()
    expect(page.get_by_text(in_use)).to_be_visible()
    assert fits_the_screen(page)


def add_git_credential_in(page: Page, label: str) -> None:
    """Whatever the page's language: by the fields' names."""
    page.goto("/git-credentials/new")
    page.locator("#label").fill(label)
    page.locator("input[name=provider][value=github]").check()
    page.locator("#token").fill(GITHUB_TOKEN)
    page.locator("form.fields button.primary").click()
