"""The pages where an Administrator grants or revokes the Privileged Projects permission, and where
a User holding it creates a Privileged Project after the warning."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, AccessProxy, connect_claude_account, wait_until

USER = "a.user@example.com"
REPOSITORY = "https://github.com/octocat/Hello-World"
ITALIAN = {"Accept-Language": "it-IT,it;q=0.9"}
WARNING = "code, Git Credentials and Claude Account"  # of every User


@pytest.fixture(autouse=True)
def public_repository(runtime: FakeRuntime) -> None:
    runtime.publish_repository(REPOSITORY)


@pytest.fixture
def administrator(proxy: AccessProxy) -> dict[str, str]:
    return proxy.headers(ADMINISTRATOR)


@pytest.fixture
def user(proxy: AccessProxy, add_user: Callable[[str], None]) -> dict[str, str]:
    add_user(USER)
    return proxy.headers(USER)


def permit(
    dockyard: TestClient, headers: dict[str, str], permitted: bool, email: str = USER
) -> Response:
    data = {"permitted": "true" if permitted else "false"}
    return dockyard.post(f"/users/{email}/privileged-projects", data=data, headers=headers)


def submit(dockyard: TestClient, headers: dict[str, str], **fields: str) -> Response:
    form = {"name": "Hello", "repository": REPOSITORY, "visibility": "public", **fields}
    return dockyard.post("/projects", data=form, headers=headers)


def test_a_user_without_the_permission_never_sees_the_privileged_option(
    dockyard: TestClient, user: dict[str, str]
) -> None:
    page = dockyard.get("/projects/new", headers=user)

    assert page.status_code == 200
    assert 'name="privileged"' not in page.text
    assert WARNING not in page.text


def test_a_user_holding_the_permission_sees_the_option_with_its_warning(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    permit(dockyard, administrator, True)

    page = dockyard.get("/projects/new", headers=user)

    assert 'name="privileged"' in page.text
    assert "equals root on the host" in page.text
    assert WARNING in page.text


def test_the_warning_is_in_italian_for_an_italian_browser(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    permit(dockyard, administrator, True)

    page = dockyard.get("/projects/new", headers={**user, **ITALIAN})

    assert "Progetto privilegiato" in page.text
    assert "equivale a root" in page.text


def test_a_project_created_privileged_from_the_form_says_so_on_its_page(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    permit(dockyard, administrator, True)
    connect_claude_account(dockyard, user)

    created = submit(dockyard, user, privileged="on")

    assert created.status_code == 200
    assert created.url.path.startswith("/projects/")
    assert "Privileged" in created.text
    assert WARNING in created.text
    (project,) = dockyard.get("/api/projects", headers=user).json()
    assert project["privileged"] is True


def test_a_project_created_from_the_form_is_not_privileged_unless_ticked(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    permit(dockyard, administrator, True)

    created = submit(dockyard, user)

    assert WARNING not in created.text
    (project,) = dockyard.get("/api/projects", headers=user).json()
    assert project["privileged"] is False


def test_a_privileged_project_submitted_without_the_permission_is_refused(
    dockyard: TestClient, user: dict[str, str]
) -> None:
    refused = submit(dockyard, user, privileged="on")

    assert refused.status_code == 403
    assert "Ask an Administrator" in refused.text
    assert dockyard.get("/api/projects", headers=user).json() == []


def test_the_user_page_grants_and_revokes_the_permission(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    before = dockyard.get(f"/users/{USER}", headers=administrator)
    assert "They cannot create Privileged Projects." in before.text
    assert "Grant the permission" in before.text

    granted = permit(dockyard, administrator, True)

    assert granted.status_code == 200
    assert granted.url.path == f"/users/{USER}"
    assert "They may create Privileged Projects." in granted.text
    assert "Revoke the permission" in granted.text
    revoked = permit(dockyard, administrator, False)
    assert "They cannot create Privileged Projects." in revoked.text


def test_revoking_from_the_user_page_stops_their_privileged_projects(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    permit(dockyard, administrator, True)
    connect_claude_account(dockyard, user)
    submit(dockyard, user, privileged="on")
    (project,) = dockyard.get("/api/projects", headers=user).json()

    def status() -> str:
        found: str = dockyard.get(f"/api/projects/{project['id']}", headers=user).json()["status"]
        return found

    assert wait_until(lambda: status() == "running")

    permit(dockyard, administrator, False)

    assert status() == "stopped"


def test_only_an_administrator_grants_the_permission_from_a_page(
    dockyard: TestClient, user: dict[str, str]
) -> None:
    refused = permit(dockyard, user, True)

    assert refused.status_code == 403
    assert 'name="privileged"' not in dockyard.get("/projects/new", headers=user).text


def test_the_users_page_shows_who_holds_the_permission(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    assert "Privileged Projects" not in dockyard.get("/users", headers=administrator).text

    permit(dockyard, administrator, True)

    assert "Privileged Projects" in dockyard.get("/users", headers=administrator).text


def test_an_administrator_sees_which_projects_are_privileged_among_all(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    permit(dockyard, administrator, True)
    submit(dockyard, user, name="Docker in Docker", privileged="on")

    page = dockyard.get("/administration/projects", headers=administrator)

    assert "Docker in Docker" in page.text
    assert ">Privileged<" in page.text
