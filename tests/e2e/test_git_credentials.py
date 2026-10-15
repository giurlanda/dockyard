"""Managing Git Credentials from the dashboard, in a browser (with the in-memory Runtime)."""

import pytest
from playwright.sync_api import Page, expect

from .conftest import ADMINISTRATOR, PHONE, OpenAs, fits_the_screen

TOKEN = "github_pat_11AAAAAAA0the0first0token0ever0saved"
NEW_TOKEN = "github_pat_11BBBBBBB0the0token0after0rotation"


def add_git_credential(page: Page, label: str, provider: str = "GitHub") -> None:
    page.goto("/")
    page.get_by_role("link", name="Add Git Credential").click()
    expect(page.get_by_text("Claude can read this token")).to_be_visible()
    page.get_by_label("Label").fill(label)
    page.get_by_label(provider).check()
    page.get_by_label("Token").fill(TOKEN)
    page.get_by_role("button", name="Save Git Credential").click()


def test_a_user_adds_renames_rotates_and_deletes_a_git_credential(open_as: OpenAs) -> None:
    page = open_as(ADMINISTRATOR)
    page.goto("/")
    expect(page.get_by_text("You have no Git Credentials yet.")).to_be_visible()

    add_git_credential(page, "Acme on GitLab", provider="GitLab")

    expect(page.get_by_role("link", name="Acme on GitLab")).to_be_visible()
    assert TOKEN not in page.content()
    page.get_by_role("link", name="Acme on GitLab").click()
    expect(page.get_by_test_id("git-credential-provider")).to_have_text("GitLab")
    assert TOKEN not in page.content()

    page.get_by_label("Label").fill("Acme")
    page.get_by_role("button", name="Rename").click()
    expect(page.get_by_text("The label has been changed.")).to_be_visible()
    expect(page.get_by_role("heading", name="Acme", exact=True)).to_be_visible()

    expect(page.get_by_text("Claude can read this token")).to_be_visible()
    page.get_by_label("New token").fill(NEW_TOKEN)
    page.get_by_role("button", name="Replace token").click()
    expect(page.get_by_text("The token has been replaced.")).to_be_visible()
    assert NEW_TOKEN not in page.content()

    page.get_by_role("button", name="Delete Git Credential").click()
    expect(page.get_by_text("You have no Git Credentials yet.")).to_be_visible()


def test_a_token_pasted_with_other_text_is_refused_and_not_shown_back(open_as: OpenAs) -> None:
    page = open_as(ADMINISTRATOR)
    page.goto("/git-credentials/new")

    page.get_by_label("Label").fill("Work")
    page.get_by_label("Token").fill("token: ghp_abc")
    page.get_by_role("button", name="Save Git Credential").click()

    expect(page.get_by_role("alert")).to_contain_text("exactly as the provider showed it")
    expect(page.get_by_label("Label")).to_have_value("Work")
    expect(page.get_by_label("Token")).to_have_value("")


@pytest.mark.parametrize(
    ("language", "add", "label", "save", "warning"),
    [
        ("English", "Add Git Credential", "Label", "Save Git Credential", "Claude can read"),
        (
            "Italiano",
            "Aggiungi Credenziale Git",
            "Etichetta",
            "Salva Credenziale Git",
            "Claude può leggere",
        ),
    ],
)
def test_on_a_phone_every_git_credential_page_fits_the_screen(
    open_as: OpenAs, language: str, add: str, label: str, save: str, warning: str
) -> None:
    page = open_as(ADMINISTRATOR, viewport=PHONE)
    page.goto("/")
    page.get_by_role("button", name=language).click()

    page.get_by_role("link", name=add).click()
    expect(page.get_by_text(warning)).to_be_visible()
    assert fits_the_screen(page)
    page.get_by_label(label).fill("A rather long label for a phone screen")
    page.get_by_label("Token").fill(TOKEN)
    page.get_by_role("button", name=save).click()
    assert fits_the_screen(page)

    page.get_by_role("link", name="A rather long label for a phone screen").click()
    expect(page.get_by_text(warning)).to_be_visible()
    assert fits_the_screen(page)
