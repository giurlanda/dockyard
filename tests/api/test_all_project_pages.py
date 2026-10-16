"""The page where an Administrator sees every Project on the Instance, and stops or deletes them."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from dockyard.runtime import ProjectStatus
from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, AccessProxy, connect_claude_account, wait_until

REPOSITORY = "https://github.com/octocat/Hello-World"
OWNER = "a.user@example.com"
ITALIAN = {"Accept-Language": "it-IT,it;q=0.9"}


@pytest.fixture(autouse=True)
def public_repository(runtime: FakeRuntime) -> None:
    runtime.publish_repository(REPOSITORY)


@pytest.fixture
def administrator(proxy: AccessProxy) -> dict[str, str]:
    return proxy.headers(ADMINISTRATOR)


@pytest.fixture
def owner(proxy: AccessProxy, add_user: Callable[[str], None]) -> dict[str, str]:
    """A User who is not an Administrator."""
    add_user(OWNER)
    return proxy.headers(OWNER)


def running_project(dockyard: TestClient, headers: dict[str, str], name: str = "Theirs") -> str:
    connect_claude_account(dockyard, headers)
    body = {"name": name, "repository": REPOSITORY, "visibility": "public"}
    project: str = dockyard.post("/api/projects", json=body, headers=headers).json()["id"]
    assert wait_until(lambda: status_of(dockyard, headers, project) == "running")
    return project


def status_of(dockyard: TestClient, headers: dict[str, str], project: str) -> str:
    status: str = dockyard.get(f"/api/projects/{project}", headers=headers).json()["status"]
    return status


def test_the_dashboard_leads_administrators_only_to_every_project(
    dockyard: TestClient, administrator: dict[str, str], owner: dict[str, str]
) -> None:
    assert 'href="/administration/projects"' in dockyard.get("/", headers=administrator).text
    assert 'href="/administration/projects"' not in dockyard.get("/", headers=owner).text


def test_the_page_lists_every_project_with_its_owner_status_and_resources(
    dockyard: TestClient, administrator: dict[str, str], owner: dict[str, str]
) -> None:
    running_project(dockyard, owner)

    page = dockyard.get("/administration/projects", headers=administrator)

    assert page.status_code == 200
    assert "Theirs" in page.text
    assert OWNER in page.text
    assert "Running" in page.text
    assert "CPUs up to 2, RAM up to 4 GiB" in page.text


def test_the_page_never_leads_to_another_users_terminal_console_or_logs(
    dockyard: TestClient, administrator: dict[str, str], owner: dict[str, str]
) -> None:
    project = running_project(dockyard, owner)

    page = dockyard.get("/administration/projects", headers=administrator).text

    assert f"/projects/{project}/terminal" not in page
    assert f"/projects/{project}/console" not in page
    assert f"/projects/{project}/logs" not in page
    assert f'href="/projects/{project}"' not in page


def test_a_project_stopped_from_the_page_shows_as_stopped(
    dockyard: TestClient, administrator: dict[str, str], owner: dict[str, str]
) -> None:
    project = running_project(dockyard, owner)

    page = dockyard.post(f"/administration/projects/{project}/stop", headers=administrator)

    assert page.status_code == 200
    assert page.url.path == "/administration/projects"
    assert "Stopped" in page.text
    assert status_of(dockyard, owner, project) == "stopped"


def test_a_project_deleted_from_the_page_disappears_and_keeps_its_code_unless_asked(
    dockyard: TestClient,
    administrator: dict[str, str],
    owner: dict[str, str],
    runtime: FakeRuntime,
) -> None:
    kept = running_project(dockyard, owner, "Kept")
    removed = running_project(dockyard, owner, "Removed")

    dockyard.post(f"/administration/projects/{kept}/delete", headers=administrator)
    page = dockyard.post(
        f"/administration/projects/{removed}/delete",
        data={"delete_code": "on"},
        headers=administrator,
    )

    assert page.status_code == 200
    assert page.url.path == "/administration/projects"
    assert "Kept" not in page.text
    assert "Removed" not in page.text
    assert runtime.project_status(kept) == ProjectStatus.STOPPED
    assert runtime.project_status(removed) == ProjectStatus.MISSING


def test_the_page_speaks_italian(
    dockyard: TestClient, administrator: dict[str, str], owner: dict[str, str]
) -> None:
    running_project(dockyard, owner)

    page = dockyard.get("/administration/projects", headers={**administrator, **ITALIAN})

    assert "Tutti i Progetti" in page.text
    assert "In esecuzione" in page.text
    assert "CPU fino a 2, RAM fino a 4 GiB" in page.text


def test_a_user_who_is_not_an_administrator_is_refused_the_page(
    dockyard: TestClient, owner: dict[str, str]
) -> None:
    page = dockyard.get("/administration/projects", headers=owner)

    assert page.status_code == 403
    assert "Administrators only" in page.text


def test_actions_from_the_page_are_refused_to_a_user_who_is_not_an_administrator(
    dockyard: TestClient, administrator: dict[str, str], owner: dict[str, str]
) -> None:
    project = running_project(dockyard, administrator, "Mine")

    stopped = dockyard.post(f"/administration/projects/{project}/stop", headers=owner)
    deleted = dockyard.post(f"/administration/projects/{project}/delete", headers=owner)

    assert (stopped.status_code, deleted.status_code) == (403, 403)
    assert status_of(dockyard, administrator, project) == "running"
