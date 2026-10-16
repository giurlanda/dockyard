"""An Administrator sees every Project on the Instance, with its owner, status and resource usage,
and stops or deletes any of them; never their Terminal, console or logs."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dockyard.runtime import ProjectStatus
from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, AccessProxy, connect_claude_account, wait_until

REPOSITORY = "https://github.com/octocat/Hello-World"
OWNER = "a.user@example.com"
GIB = 1024**3


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


def create(dockyard: TestClient, headers: dict[str, str], name: str, **fields: Any) -> str:
    body = {"name": name, "repository": REPOSITORY, "visibility": "public", **fields}
    created = dockyard.post("/api/projects", json=body, headers=headers)
    assert created.status_code == 202, created.text
    project: str = created.json()["id"]
    return project


def status_of(dockyard: TestClient, headers: dict[str, str], project: str) -> str:
    status: str = dockyard.get(f"/api/projects/{project}", headers=headers).json()["status"]
    return status


def running_project(dockyard: TestClient, headers: dict[str, str], name: str = "Hello") -> str:
    connect_claude_account(dockyard, headers)
    project = create(dockyard, headers, name)
    assert wait_until(lambda: status_of(dockyard, headers, project) == "running")
    return project


def test_an_administrator_lists_every_project_with_its_owner_status_and_resources(
    dockyard: TestClient, administrator: dict[str, str], owner: dict[str, str]
) -> None:
    theirs = running_project(dockyard, owner, "Theirs")
    mine = create(dockyard, administrator, "Mine", cpus=1, memory=2 * GIB)
    assert wait_until(lambda: status_of(dockyard, administrator, mine) == "waiting")

    listed = dockyard.get("/api/administration/projects", headers=administrator)

    assert listed.status_code == 200
    found = {project["id"]: project for project in listed.json()}
    assert found.keys() == {theirs, mine}
    assert found[theirs]["owner"] == OWNER
    assert found[theirs]["name"] == "Theirs"
    assert found[theirs]["status"] == "running"
    assert (found[theirs]["cpus"], found[theirs]["memory"]) == (2, 4 * GIB)
    assert found[mine]["owner"] == ADMINISTRATOR
    assert found[mine]["status"] == "waiting"
    assert (found[mine]["cpus"], found[mine]["memory"]) == (1, 2 * GIB)


def test_an_administrator_stops_another_users_project(
    dockyard: TestClient, administrator: dict[str, str], owner: dict[str, str]
) -> None:
    project = running_project(dockyard, owner)

    stopped = dockyard.post(f"/api/administration/projects/{project}/stop", headers=administrator)

    assert stopped.status_code == 200
    assert stopped.json()["status"] == "stopped"
    assert stopped.json()["owner"] == OWNER
    assert status_of(dockyard, owner, project) == "stopped"


def test_an_administrator_deletes_another_users_project_keeping_its_code_unless_asked(
    dockyard: TestClient,
    administrator: dict[str, str],
    owner: dict[str, str],
    runtime: FakeRuntime,
) -> None:
    kept = running_project(dockyard, owner, "Kept")
    removed = running_project(dockyard, owner, "Removed")

    first = dockyard.delete(f"/api/administration/projects/{kept}", headers=administrator)
    second = dockyard.delete(
        f"/api/administration/projects/{removed}?delete_code=true", headers=administrator
    )

    assert (first.status_code, second.status_code) == (204, 204)
    assert dockyard.get("/api/projects", headers=owner).json() == []
    assert dockyard.get("/api/administration/projects", headers=administrator).json() == []
    assert runtime.project_status(kept) == ProjectStatus.STOPPED
    assert runtime.project_status(removed) == ProjectStatus.MISSING


def test_an_administrator_sees_the_cpu_and_ram_another_users_project_uses_now(
    dockyard: TestClient,
    administrator: dict[str, str],
    owner: dict[str, str],
    runtime: FakeRuntime,
) -> None:
    project = running_project(dockyard, owner)
    runtime.use(project, cpu=37.5, memory=512 * 1024**2)

    stats = dockyard.get(f"/api/administration/projects/{project}/stats", headers=administrator)

    assert stats.status_code == 200
    assert stats.json() == {
        "cpu": 37.5,
        "cpus": 2,
        "memory": 512 * 1024**2,
        "memory_limit": 4 * GIB,
    }


def test_a_stopped_project_has_no_stats_for_an_administrator_either(
    dockyard: TestClient, administrator: dict[str, str], owner: dict[str, str]
) -> None:
    project = running_project(dockyard, owner)
    dockyard.post(f"/api/projects/{project}/stop", headers=owner)

    stats = dockyard.get(f"/api/administration/projects/{project}/stats", headers=administrator)

    assert stats.status_code == 409
    assert stats.json()["error"] == "not_running"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/administration/projects"),
        ("GET", "/api/administration/projects/{project}/stats"),
        ("POST", "/api/administration/projects/{project}/stop"),
        ("DELETE", "/api/administration/projects/{project}?delete_code=true"),
    ],
)
def test_a_user_who_is_not_an_administrator_is_refused(
    dockyard: TestClient,
    administrator: dict[str, str],
    owner: dict[str, str],
    method: str,
    path: str,
) -> None:
    project = running_project(dockyard, administrator)

    refused = dockyard.request(method, path.format(project=project), headers=owner)

    assert refused.status_code == 403
    assert refused.json()["error"] == "administrator_only"
    assert status_of(dockyard, administrator, project) == "running"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/administration/projects/missing/stats"),
        ("POST", "/api/administration/projects/missing/stop"),
        ("DELETE", "/api/administration/projects/missing"),
    ],
)
def test_a_project_that_does_not_exist_is_not_found(
    dockyard: TestClient, administrator: dict[str, str], method: str, path: str
) -> None:
    assert dockyard.request(method, path, headers=administrator).status_code == 404
