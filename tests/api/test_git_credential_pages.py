"""The pages where a User adds, renames, rotates and deletes their Git Credentials."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from .conftest import ADMINISTRATOR, AccessProxy

TOKEN = "github_pat_11AAAAAAA0the0first0token0ever0saved"
NEW_TOKEN = "github_pat_11BBBBBBB0the0token0after0rotation"
OTHER_USER = "another.user@example.com"
ITALIAN = {"Accept-Language": "it-IT,it;q=0.9"}


def submit(
    dockyard: TestClient,
    headers: dict[str, str],
    *,
    label: str = "Work account",
    provider: str = "github",
    token: str = TOKEN,
) -> Response:
    return dockyard.post(
        "/git-credentials",
        data={"label": label, "provider": provider, "token": token},
        headers=headers,
    )


def credential_id(dockyard: TestClient, headers: dict[str, str], label: str) -> str:
    listed = dockyard.get("/api/git-credentials", headers=headers).json()
    (found,) = (c["id"] for c in listed if c["label"] == label)
    return str(found)


def assert_warns_about_the_token(page: str) -> None:
    assert "Claude can read this token" in page
    assert "fine-grained" in page
    assert "expiry" in page


def test_the_dashboard_leads_to_the_form_that_adds_a_git_credential(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    dashboard = dockyard.get("/", headers=signed_in).text
    assert "Git Credentials" in dashboard
    assert 'href="/git-credentials/new"' in dashboard

    form = dockyard.get("/git-credentials/new", headers=signed_in)

    assert form.status_code == 200
    assert "Label" in form.text
    assert "GitHub" in form.text
    assert "GitLab" in form.text
    assert 'type="password"' in form.text
    assert_warns_about_the_token(form.text)


def test_a_saved_git_credential_appears_on_the_dashboard_by_label_and_provider_only(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)

    page = submit(dockyard, signed_in, label="Acme on GitLab", provider="gitlab")

    assert page.status_code == 200
    assert page.url.path == "/"
    assert "Acme on GitLab" in page.text
    assert "GitLab" in page.text
    assert TOKEN not in page.text
    credential = credential_id(dockyard, signed_in, "Acme on GitLab")
    assert TOKEN not in dockyard.get(f"/git-credentials/{credential}", headers=signed_in).text


def test_a_refused_git_credential_shows_why_and_keeps_the_label_but_not_the_token(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    pasted = "ghp_pasted with a space"

    page = submit(dockyard, signed_in, label="Work account", token=pasted)

    assert page.status_code == 422
    assert "exactly as" in page.text
    assert 'value="Work account"' in page.text
    assert pasted not in page.text
    assert_warns_about_the_token(page.text)
    assert dockyard.get("/api/git-credentials", headers=signed_in).json() == []


def test_a_user_renames_a_git_credential_from_its_page(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    submit(dockyard, signed_in, label="Work")
    credential = credential_id(dockyard, signed_in, "Work")

    page = dockyard.post(
        f"/git-credentials/{credential}/label", data={"label": "Acme"}, headers=signed_in
    )

    assert page.status_code == 200
    assert page.url.path == f"/git-credentials/{credential}"
    assert "Acme" in page.text
    assert "The label has been changed." in page.text


def test_a_user_replaces_the_token_from_the_credentials_page_with_the_warning_in_view(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    submit(dockyard, signed_in, label="Work")
    credential = credential_id(dockyard, signed_in, "Work")
    assert_warns_about_the_token(
        dockyard.get(f"/git-credentials/{credential}", headers=signed_in).text
    )

    page = dockyard.post(
        f"/git-credentials/{credential}/token", data={"token": NEW_TOKEN}, headers=signed_in
    )

    assert page.status_code == 200
    assert page.url.path == f"/git-credentials/{credential}"
    assert "The token has been replaced." in page.text
    assert NEW_TOKEN not in page.text
    assert TOKEN not in page.text


@pytest.mark.parametrize(
    ("action", "data", "message"),
    [
        ("label", {"label": ""}, "Give the Git Credential a label"),
        ("token", {"token": "not a token"}, "exactly as"),
    ],
)
def test_a_refused_rename_or_rotation_shows_why_on_the_credentials_page(
    dockyard: TestClient, proxy: AccessProxy, action: str, data: dict[str, str], message: str
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    submit(dockyard, signed_in, label="Work")
    credential = credential_id(dockyard, signed_in, "Work")

    page = dockyard.post(f"/git-credentials/{credential}/{action}", data=data, headers=signed_in)

    assert page.status_code == 422
    assert message in page.text
    assert "not a token" not in page.text
    assert credential_id(dockyard, signed_in, "Work") == credential


def test_a_user_deletes_a_git_credential_from_its_page(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    submit(dockyard, signed_in, label="Work")
    credential = credential_id(dockyard, signed_in, "Work")

    page = dockyard.post(f"/git-credentials/{credential}/delete", headers=signed_in)

    assert page.status_code == 200
    assert page.url.path == "/"
    assert dockyard.get("/api/git-credentials", headers=signed_in).json() == []
    assert dockyard.get(f"/git-credentials/{credential}", headers=signed_in).status_code == 404


def test_another_users_git_credential_pages_are_not_found(
    dockyard: TestClient, proxy: AccessProxy, add_user: Callable[[str], None]
) -> None:
    add_user(OTHER_USER)
    owner = proxy.headers(ADMINISTRATOR)
    other = proxy.headers(OTHER_USER)
    submit(dockyard, owner, label="Mine")
    credential = credential_id(dockyard, owner, "Mine")
    page = f"/git-credentials/{credential}"

    assert "Mine" not in dockyard.get("/", headers=other).text
    assert dockyard.get(page, headers=other).status_code == 404
    assert dockyard.post(f"{page}/label", data={"label": "x"}, headers=other).status_code == 404
    assert dockyard.post(f"{page}/token", data={"token": "ghp_x"}, headers=other).status_code == 404
    assert dockyard.post(f"{page}/delete", headers=other).status_code == 404
    assert credential_id(dockyard, owner, "Mine") == credential


def test_the_warning_about_the_token_is_in_italian_for_an_italian_browser(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    form = dockyard.get("/git-credentials/new", headers={**proxy.headers(ADMINISTRATOR), **ITALIAN})

    assert "Credenziale Git" in form.text
    assert "Claude può leggere questo token" in form.text
