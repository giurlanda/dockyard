"""The pages where a User creates a private Project with a Git Credential, and sees which Projects
use a Git Credential."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, AccessProxy, wait_until

PRIVATE_REPOSITORY = "https://github.com/acme/secret-sauce"
TOKEN = "github_pat_11AAAAAAA0the0private0repository0token"
OTHER_USER = "another.user@example.com"
ITALIAN = {"Accept-Language": "it-IT,it;q=0.9"}


@pytest.fixture(autouse=True)
def private_repository(runtime: FakeRuntime) -> None:
    runtime.publish_repository(PRIVATE_REPOSITORY, token=TOKEN)


@pytest.fixture
def signed_in(proxy: AccessProxy) -> dict[str, str]:
    return proxy.headers(ADMINISTRATOR)


def add_credential(
    dockyard: TestClient,
    headers: dict[str, str],
    *,
    label: str = "Acme",
    provider: str = "github",
    token: str = TOKEN,
) -> str:
    body = {"label": label, "provider": provider, "token": token}
    added = dockyard.post("/api/git-credentials", json=body, headers=headers)
    return str(added.json()["id"])


def submit(dockyard: TestClient, headers: dict[str, str], **fields: str) -> Response:
    form = {
        "name": "Secret",
        "repository": PRIVATE_REPOSITORY,
        "visibility": "private",
        "git_credential": "",
        **fields,
    }
    return dockyard.post("/projects", data=form, headers=headers)


def test_the_new_project_form_offers_the_users_git_credentials_by_provider(
    dockyard: TestClient,
    proxy: AccessProxy,
    signed_in: dict[str, str],
    add_user: Callable[[str], None],
) -> None:
    add_user(OTHER_USER)
    add_credential(dockyard, proxy.headers(OTHER_USER), label="Not mine")
    github = add_credential(dockyard, signed_in, label="Acme on GitHub")
    gitlab = add_credential(
        dockyard, signed_in, label="Acme on GitLab", provider="gitlab", token="glpat-acme"
    )

    form = dockyard.get("/projects/new", headers=signed_in).text

    assert "Private" in form
    assert '<optgroup label="GitHub" data-provider="github">' in form
    assert '<optgroup label="GitLab" data-provider="gitlab">' in form
    assert f'<option value="{github}">Acme on GitHub</option>' in form
    assert f'<option value="{gitlab}">Acme on GitLab</option>' in form
    assert "Not mine" not in form
    assert "Claude can read this token" in form
    assert TOKEN not in form


def test_without_git_credentials_the_form_leads_to_adding_one(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    form = dockyard.get("/projects/new", headers=signed_in).text

    assert "a private Project needs one" in form
    assert 'href="/git-credentials/new"' in form


def test_a_private_project_without_a_git_credential_is_explained_on_the_form(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    page = submit(dockyard, signed_in)

    assert page.status_code == 422
    assert "choose one of your Git Credentials" in page.text
    assert 'value="private" checked' in page.text


def test_a_private_project_created_from_the_form_shows_its_git_credential(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    credential = add_credential(dockyard, signed_in, label="Acme")

    page = submit(dockyard, signed_in, git_credential=credential)

    assert page.status_code == 200
    assert page.url.path.startswith("/projects/")
    assert "Private" in page.text
    assert f'<a href="/git-credentials/{credential}">Acme</a>' in page.text
    assert TOKEN not in page.text


def test_a_failed_private_clone_is_retried_with_the_same_visibility_and_git_credential(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    credential = add_credential(dockyard, signed_in, token="github_pat_11revoked0token")
    page = submit(dockyard, signed_in, git_credential=credential)
    project = page.url.path

    assert wait_until(lambda: "The clone failed" in dockyard.get(project, headers=signed_in).text)
    failed = dockyard.get(project, headers=signed_in).text
    assert "Authentication failed" in failed
    retry = dockyard.get(
        f"/projects/new?name=Secret&repository={PRIVATE_REPOSITORY}"
        f"&visibility=private&git_credential={credential}",
        headers=signed_in,
    ).text
    assert f"git_credential={credential}" in failed
    assert 'value="private" checked' in retry
    assert f'<option value="{credential}" selected>' in retry


def test_the_git_credential_page_names_the_projects_using_it_and_refuses_deleting_it(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    credential = add_credential(dockyard, signed_in)
    submit(dockyard, signed_in, name="Secret sauce", git_credential=credential)
    path = f"/git-credentials/{credential}"

    page = dockyard.get(path, headers=signed_in).text
    refused = dockyard.post(f"{path}/delete", headers=signed_in)

    assert "Secret sauce" in page
    assert "Delete these Projects first" in page
    assert "Delete Git Credential</button>" not in page
    assert "next start" in page
    assert refused.status_code == 409
    assert "cannot be deleted" in refused.text
    assert "Secret sauce" in refused.text
    assert dockyard.get(path, headers=signed_in).status_code == 200


def test_the_private_project_pages_speak_italian(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    italian = {**signed_in, **ITALIAN}
    credential = add_credential(dockyard, signed_in)

    form = dockyard.get("/projects/new", headers=italian).text
    refused = submit(dockyard, italian)
    project = submit(dockyard, italian, git_credential=credential)
    in_use = dockyard.get(f"/git-credentials/{credential}", headers=italian).text

    assert "Privato" in form
    assert "Credenziale Git" in form
    assert "scegli una delle tue Credenziali Git" in refused.text
    assert "Privato" in project.text
    assert "prossimo avvio" in project.text
    assert "Elimina prima questi Progetti" in in_use
