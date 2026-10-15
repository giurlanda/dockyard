"""The pages where an Administrator invites people and manages Users."""

from collections.abc import Callable
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from dockyard.runtime import ProjectStatus
from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, AccessProxy, wait_until

USER = "a.user@example.com"
INVITED = "invited.person@example.com"
REPOSITORY = "https://github.com/octocat/Hello-World"
ITALIAN = {"Accept-Language": "it-IT,it;q=0.9"}
CONFIRMED = {"confirm": "on"}


@pytest.fixture
def signed_in(proxy: AccessProxy) -> dict[str, str]:
    return proxy.headers(ADMINISTRATOR)


def test_the_dashboard_leads_administrators_only_to_the_users_page(
    dockyard: TestClient,
    proxy: AccessProxy,
    signed_in: dict[str, str],
    add_user: Callable[[str], None],
) -> None:
    add_user(USER)

    assert 'href="/users"' in dockyard.get("/", headers=signed_in).text
    assert 'href="/users"' not in dockyard.get("/", headers=proxy.headers(USER)).text


def test_the_users_page_lists_users_with_their_role_and_pending_invitations(
    dockyard: TestClient, signed_in: dict[str, str], add_user: Callable[[str], None]
) -> None:
    add_user(USER)
    dockyard.post("/invitations", data={"email": INVITED}, headers=signed_in)

    page = dockyard.get("/users", headers=signed_in)

    assert page.status_code == 200
    assert ADMINISTRATOR in page.text
    assert USER in page.text
    assert "Administrator" in page.text
    assert INVITED in page.text
    assert "Pending Invitations" in page.text


def test_an_invitation_made_from_the_form_appears_on_the_users_page(
    dockyard: TestClient, proxy: AccessProxy, signed_in: dict[str, str]
) -> None:
    page = dockyard.post("/invitations", data={"email": INVITED}, headers=signed_in)

    assert page.status_code == 200
    assert page.url.path == "/users"
    assert INVITED in page.text
    assert dockyard.get("/api/me", headers=proxy.headers(INVITED)).status_code == 200


def test_a_refused_invitation_shows_why_and_keeps_what_was_typed(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    page = dockyard.post("/invitations", data={"email": "someone at example"}, headers=signed_in)

    assert page.status_code == 422
    assert "not an email address" in page.text
    assert 'value="someone at example"' in page.text


def test_an_invitation_revoked_from_the_users_page_disappears(
    dockyard: TestClient, proxy: AccessProxy, signed_in: dict[str, str]
) -> None:
    dockyard.post("/invitations", data={"email": INVITED}, headers=signed_in)

    page = dockyard.post(f"/invitations/{INVITED}/revoke", headers=signed_in)

    assert page.status_code == 200
    assert page.url.path == "/users"
    assert INVITED not in page.text
    assert dockyard.get("/api/me", headers=proxy.headers(INVITED)).status_code == 403


def test_an_administrator_promotes_and_demotes_a_user_from_their_page(
    dockyard: TestClient,
    proxy: AccessProxy,
    signed_in: dict[str, str],
    add_user: Callable[[str], None],
) -> None:
    add_user(USER)
    assert f'href="/users/{quote(USER)}"' in dockyard.get("/users", headers=signed_in).text

    promoted = dockyard.post(
        f"/users/{USER}/role", data={"role": "administrator"}, headers=signed_in
    )

    assert promoted.url.path == f"/users/{USER}"
    assert "is now an Administrator" in promoted.text
    assert dockyard.get("/api/me", headers=proxy.headers(USER)).json()["role"] == "administrator"
    demoted = dockyard.post(f"/users/{USER}/role", data={"role": "user"}, headers=signed_in)
    assert "is no longer an Administrator" in demoted.text
    assert dockyard.get("/api/me", headers=proxy.headers(USER)).json()["role"] == "user"


def test_the_page_of_the_last_administrator_explains_why_they_cannot_be_demoted(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    page = dockyard.post(f"/users/{ADMINISTRATOR}/role", data={"role": "user"}, headers=signed_in)

    assert page.status_code == 409
    assert "last Administrator" in page.text
    assert dockyard.get("/api/me", headers=signed_in).json()["role"] == "administrator"


def test_the_user_page_names_the_projects_that_removing_them_deletes(
    dockyard: TestClient,
    proxy: AccessProxy,
    runtime: FakeRuntime,
    signed_in: dict[str, str],
    add_user: Callable[[str], None],
) -> None:
    add_user(USER)
    runtime.publish_repository(REPOSITORY)
    body = {"name": "Their Project", "repository": REPOSITORY, "visibility": "public"}
    project = dockyard.post("/api/projects", json=body, headers=proxy.headers(USER)).json()["id"]
    assert wait_until(lambda: runtime.project_status(project) == ProjectStatus.STOPPED)

    page = dockyard.get(f"/users/{USER}", headers=signed_in)

    assert "Their Project" in page.text
    assert "cannot be undone" in page.text

    removed = dockyard.post(f"/users/{USER}/remove", data=CONFIRMED, headers=signed_in)

    assert removed.status_code == 200
    assert removed.url.path == "/users"
    assert USER not in removed.text
    assert runtime.project_status(project) == ProjectStatus.MISSING
    assert dockyard.get("/api/me", headers=proxy.headers(USER)).status_code == 403


def test_a_user_is_not_removed_from_their_page_without_confirmation(
    dockyard: TestClient,
    proxy: AccessProxy,
    signed_in: dict[str, str],
    add_user: Callable[[str], None],
) -> None:
    add_user(USER)

    page = dockyard.post(f"/users/{USER}/remove", headers=signed_in)

    assert page.status_code == 422
    assert "Tick the box" in page.text
    assert dockyard.get("/api/me", headers=proxy.headers(USER)).status_code == 200


def test_the_last_administrator_cannot_be_removed_from_their_page(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    page = dockyard.post(f"/users/{ADMINISTRATOR}/remove", data=CONFIRMED, headers=signed_in)

    assert page.status_code == 409
    assert "last Administrator" in page.text
    assert dockyard.get("/api/me", headers=signed_in).status_code == 200


def test_the_page_of_someone_who_is_not_a_user_is_not_found(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    assert dockyard.get("/users/nobody@example.com", headers=signed_in).status_code == 404
    gone = dockyard.post("/users/nobody@example.com/remove", data=CONFIRMED, headers=signed_in)
    assert gone.status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/users"),
        ("GET", f"/users/{ADMINISTRATOR}"),
        ("POST", "/invitations"),
        ("POST", f"/invitations/{INVITED}/revoke"),
        ("POST", f"/users/{ADMINISTRATOR}/role"),
        ("POST", f"/users/{ADMINISTRATOR}/remove"),
        ("GET", "/api/users"),
        ("PATCH", f"/api/users/{ADMINISTRATOR}"),
        ("DELETE", f"/api/users/{ADMINISTRATOR}"),
        ("GET", "/api/invitations"),
        ("POST", "/api/invitations"),
        ("DELETE", f"/api/invitations/{INVITED}"),
    ],
)
def test_a_user_who_is_not_an_administrator_reaches_none_of_it(
    dockyard: TestClient,
    proxy: AccessProxy,
    signed_in: dict[str, str],
    add_user: Callable[[str], None],
    method: str,
    path: str,
) -> None:
    add_user(USER)
    dockyard.post("/invitations", data={"email": INVITED}, headers=signed_in)

    refused = dockyard.request(
        method,
        path,
        data={"email": "friend@example.com", "role": "user"}
        if not path.startswith("/api")
        else None,
        json={"email": "friend@example.com", "role": "user"} if path.startswith("/api") else None,
        headers=proxy.headers(USER),
    )

    assert refused.status_code == 403
    assert "Only an Administrator" in refused.text
    assert dockyard.get("/api/me", headers=signed_in).json()["role"] == "administrator"
    invitations = dockyard.get("/api/invitations", headers=signed_in).json()
    assert [i["email"] for i in invitations] == [INVITED]


def test_the_users_page_is_in_italian_for_an_italian_browser(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    page = dockyard.get("/users", headers={**signed_in, **ITALIAN})

    assert "Utenti" in page.text
    assert "Invita" in page.text
    assert "Amministratore" in page.text
