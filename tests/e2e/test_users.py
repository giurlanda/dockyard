"""Inviting people and managing Users, in a browser (with the in-memory Runtime)."""

import pytest
from playwright.sync_api import Page, expect

from .conftest import ADMINISTRATOR, PHONE, OpenAs, fits_the_screen

INVITED = "invited.person@dockyard.example.com"


def invite(page: Page, email: str) -> None:
    page.goto("/")
    page.get_by_role("link", name="Users and Invitations").click()
    page.get_by_label("Email").fill(email)
    page.get_by_role("button", name="Invite").click()


def test_an_invited_person_lands_on_their_dashboard_and_appears_among_users(
    open_as: OpenAs,
) -> None:
    administrator = open_as(ADMINISTRATOR)
    invite(administrator, INVITED)
    pending = administrator.get_by_role("region", name="Pending Invitations")
    expect(pending.get_by_text(INVITED)).to_be_visible()

    person = open_as(INVITED)
    person.goto("/")
    expect(person.get_by_test_id("user-email")).to_have_text(INVITED)
    expect(person.get_by_test_id("user-role")).to_have_text("User")
    expect(person.get_by_role("heading", name="Projects")).to_be_visible()
    expect(person.get_by_role("link", name="Users and Invitations")).to_have_count(0)

    administrator.reload()
    expect(pending.get_by_text("No Invitations are waiting")).to_be_visible()
    users = administrator.get_by_role("region", name="Users", exact=True)
    expect(users.get_by_role("link", name=INVITED)).to_be_visible()


def test_a_revoked_invitation_lets_nobody_in(open_as: OpenAs) -> None:
    administrator = open_as(ADMINISTRATOR)
    invite(administrator, INVITED)

    administrator.get_by_role("button", name=f"Revoke the Invitation of {INVITED}").click()

    expect(administrator.get_by_text("No Invitations are waiting")).to_be_visible()
    person = open_as(INVITED)
    person.goto("/")
    expect(person.get_by_role("heading", name="Not invited")).to_be_visible()


def test_an_administrator_promotes_demotes_and_removes_a_user(open_as: OpenAs) -> None:
    administrator = open_as(ADMINISTRATOR)
    invite(administrator, INVITED)
    person = open_as(INVITED)
    person.goto("/")
    administrator.reload()

    administrator.get_by_role("link", name=INVITED).click()
    administrator.get_by_role("button", name="Make Administrator").click()
    expect(administrator.get_by_role("status")).to_have_text(f"{INVITED} is now an Administrator.")
    person.reload()
    expect(person.get_by_test_id("user-role")).to_have_text("Administrator")

    administrator.get_by_role("button", name="Remove the Administrator role").click()
    expect(administrator.get_by_role("status")).to_have_text(
        f"{INVITED} is no longer an Administrator."
    )

    expect(administrator.get_by_text("It cannot be undone.")).to_be_visible()
    administrator.get_by_label("I understand that this cannot be undone").check()
    administrator.get_by_role("button", name="Remove User").click()
    expect(administrator.get_by_role("heading", name="Users", exact=True)).to_be_visible()
    expect(administrator.get_by_role("link", name=INVITED)).to_have_count(0)
    person.reload()
    expect(person.get_by_role("heading", name="Not invited")).to_be_visible()


def test_the_last_administrator_is_told_why_they_cannot_be_demoted(open_as: OpenAs) -> None:
    administrator = open_as(ADMINISTRATOR)
    administrator.goto("/users")

    administrator.get_by_role("link", name=ADMINISTRATOR).click()
    administrator.get_by_role("button", name="Remove the Administrator role").click()

    expect(administrator.get_by_role("alert")).to_contain_text("last Administrator")
    expect(administrator.get_by_test_id("user-role")).to_have_text("Administrator")


def test_a_user_who_is_not_an_administrator_is_refused_the_users_page(open_as: OpenAs) -> None:
    invite(open_as(ADMINISTRATOR), INVITED)
    person = open_as(INVITED)

    person.goto("/users")

    expect(person.get_by_role("heading", name="Administrators only")).to_be_visible()


@pytest.mark.parametrize(
    ("language", "link", "button"),
    [
        ("English", "Users and Invitations", "Make Administrator"),
        ("Italiano", "Utenti e Inviti", "Rendi Amministratore"),
    ],
)
def test_on_a_phone_the_users_pages_fit_the_screen(
    open_as: OpenAs, language: str, link: str, button: str
) -> None:
    long_email = "someone.with.a.rather.long.address@a-long-organisation-name.example.com"
    administrator = open_as(ADMINISTRATOR, viewport=PHONE)
    invite(administrator, long_email)
    open_as(long_email).goto("/")
    administrator.goto("/")
    administrator.get_by_role("button", name=language).click()

    administrator.get_by_role("link", name=link).click()
    expect(administrator.get_by_role("link", name=long_email)).to_be_visible()
    assert fits_the_screen(administrator)

    administrator.get_by_role("link", name=long_email).click()
    expect(administrator.get_by_role("button", name=button)).to_be_visible()
    assert fits_the_screen(administrator)
