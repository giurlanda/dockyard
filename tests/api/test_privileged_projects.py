"""Privileged Projects: an Administrator grants or revokes the permission per User; only a User
holding it obtains a Project whose environment runs privileged, which equals root on the host."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, AccessProxy, connect_claude_account, wait_until

USER = "a.user@example.com"
REPOSITORY = "https://github.com/octocat/Hello-World"


@pytest.fixture(autouse=True)
def public_repository(runtime: FakeRuntime) -> None:
    runtime.publish_repository(REPOSITORY)


def permit(
    dockyard: TestClient, proxy: AccessProxy, email: str, permitted: bool, by: str = ADMINISTRATOR
) -> Any:
    return dockyard.patch(
        f"/api/users/{email}", json={"privileged_projects": permitted}, headers=proxy.headers(by)
    )


def permitted(dockyard: TestClient, proxy: AccessProxy, email: str) -> bool:
    users = dockyard.get("/api/users", headers=proxy.headers(ADMINISTRATOR)).json()
    (found,) = [user for user in users if user["email"] == email]
    held: bool = found["privileged_projects"]
    return held


def create(dockyard: TestClient, headers: dict[str, str], **fields: Any) -> Any:
    body = {"name": "Hello", "repository": REPOSITORY, "visibility": "public", **fields}
    return dockyard.post("/api/projects", json=body, headers=headers)


def status(dockyard: TestClient, headers: dict[str, str], project: str) -> str:
    found: str = dockyard.get(f"/api/projects/{project}", headers=headers).json()["status"]
    return found


def running(dockyard: TestClient, headers: dict[str, str], project: str) -> bool:
    return wait_until(lambda: status(dockyard, headers, project) == "running")


def runs_privileged(runtime: FakeRuntime, project: str) -> bool:
    """As the environment's own report says: hardened ones have no new privileges."""
    log = runtime.project_log(project)
    assert "no new privileges: " in log, log
    return "no new privileges: 0" in log


def test_nobody_holds_the_permission_until_an_administrator_grants_it(
    dockyard: TestClient, proxy: AccessProxy, add_user: Callable[[str], None]
) -> None:
    add_user(USER)

    assert not permitted(dockyard, proxy, USER)
    assert not permitted(dockyard, proxy, ADMINISTRATOR)  # not even an Administrator


def test_an_administrator_grants_and_revokes_the_permission(
    dockyard: TestClient, proxy: AccessProxy, add_user: Callable[[str], None]
) -> None:
    add_user(USER)

    granted = permit(dockyard, proxy, USER, True)

    assert granted.status_code == 200
    assert granted.json() == {"email": USER, "role": "user", "privileged_projects": True}
    assert permitted(dockyard, proxy, USER)
    revoked = permit(dockyard, proxy, USER, False)
    assert revoked.json()["privileged_projects"] is False
    assert not permitted(dockyard, proxy, USER)


def test_only_an_administrator_grants_the_permission(
    dockyard: TestClient, proxy: AccessProxy, add_user: Callable[[str], None]
) -> None:
    add_user(USER)

    refused = permit(dockyard, proxy, USER, True, by=USER)

    assert refused.status_code == 403
    assert not permitted(dockyard, proxy, USER)


def test_granting_the_permission_to_nobody_is_not_found(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    assert permit(dockyard, proxy, "nobody@example.com", True).status_code == 404


def test_a_user_without_the_permission_is_refused_a_privileged_project(
    dockyard: TestClient, proxy: AccessProxy, add_user: Callable[[str], None]
) -> None:
    add_user(USER)
    headers = proxy.headers(USER)

    refused = create(dockyard, headers, privileged=True)

    assert refused.status_code == 403
    assert refused.json()["error"] == "privileged_not_permitted"
    assert "Administrator" in refused.json()["message"]
    assert dockyard.get("/api/projects", headers=headers).json() == []


def test_a_privileged_project_runs_privileged_while_the_others_stay_hardened(
    dockyard: TestClient, proxy: AccessProxy, runtime: FakeRuntime, add_user: Callable[[str], None]
) -> None:
    add_user(USER)
    permit(dockyard, proxy, USER, True)
    headers = proxy.headers(USER)
    connect_claude_account(dockyard, headers)

    privileged = create(dockyard, headers, name="Docker in Docker", privileged=True).json()
    hardened = create(dockyard, headers, name="Plain").json()

    assert (privileged["privileged"], hardened["privileged"]) == (True, False)
    assert running(dockyard, headers, privileged["id"])
    assert running(dockyard, headers, hardened["id"])
    assert runs_privileged(runtime, privileged["id"])
    assert not runs_privileged(runtime, hardened["id"])


def test_revoking_the_permission_stops_the_users_privileged_projects_until_granted_again(
    dockyard: TestClient, proxy: AccessProxy, runtime: FakeRuntime, add_user: Callable[[str], None]
) -> None:
    add_user(USER)
    permit(dockyard, proxy, USER, True)
    headers = proxy.headers(USER)
    connect_claude_account(dockyard, headers)
    privileged = create(dockyard, headers, privileged=True).json()["id"]
    hardened = create(dockyard, headers, name="Plain").json()["id"]
    assert running(dockyard, headers, privileged) and running(dockyard, headers, hardened)

    permit(dockyard, proxy, USER, False)

    assert status(dockyard, headers, privileged) == "stopped"
    assert status(dockyard, headers, hardened) == "running"
    refused = dockyard.post(f"/api/projects/{privileged}/start", headers=headers)
    assert refused.status_code == 403
    assert refused.json()["error"] == "privileged_not_permitted"
    assert status(dockyard, headers, privileged) == "stopped"
    permit(dockyard, proxy, USER, True)
    assert dockyard.post(f"/api/projects/{privileged}/start", headers=headers).status_code == 200
    assert running(dockyard, headers, privileged)
    assert runs_privileged(runtime, privileged)


def test_a_privileged_project_waiting_for_its_claude_account_never_starts_once_revoked(
    dockyard: TestClient, proxy: AccessProxy, runtime: FakeRuntime, add_user: Callable[[str], None]
) -> None:
    add_user(USER)
    permit(dockyard, proxy, USER, True)
    headers = proxy.headers(USER)
    project = create(dockyard, headers, privileged=True).json()["id"]
    assert wait_until(lambda: status(dockyard, headers, project) == "waiting")

    permit(dockyard, proxy, USER, False)
    connect_claude_account(dockyard, headers)

    assert status(dockyard, headers, project) == "stopped"
    assert runtime.project_log(project) == ""


def test_a_revocation_while_docker_is_unreachable_stops_the_privileged_projects_once_it_is_back(
    dockyard: TestClient, proxy: AccessProxy, runtime: FakeRuntime, add_user: Callable[[str], None]
) -> None:
    add_user(USER)
    permit(dockyard, proxy, USER, True)
    headers = proxy.headers(USER)
    connect_claude_account(dockyard, headers)
    project = create(dockyard, headers, privileged=True).json()["id"]
    assert running(dockyard, headers, project)

    with runtime.unavailable():
        revoked = permit(dockyard, proxy, USER, False)

    assert revoked.status_code == 200
    assert not permitted(dockyard, proxy, USER)  # no new start while Docker is away
    assert wait_until(lambda: status(dockyard, headers, project) == "stopped")
    assert runtime.project_log(project) == ""
