"""An Administrator manages Users: promotes, demotes and removes them, never the last
Administrator."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from dockyard.runtime import ProjectStatus
from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, AccessProxy, connect_claude_account, wait_until

USER = "a.user@example.com"
REPOSITORY = "https://github.com/octocat/Hello-World"


def change_role(
    dockyard: TestClient, proxy: AccessProxy, email: str, role: str, by: str = ADMINISTRATOR
) -> Any:
    return dockyard.patch(f"/api/users/{email}", json={"role": role}, headers=proxy.headers(by))


def role_of(dockyard: TestClient, proxy: AccessProxy, email: str) -> str:
    role: str = dockyard.get("/api/me", headers=proxy.headers(email)).json()["role"]
    return role


def test_a_promoted_user_becomes_an_administrator_and_can_invite(
    dockyard: TestClient, proxy: AccessProxy, add_user: Callable[[str], None]
) -> None:
    add_user(USER)

    promoted = change_role(dockyard, proxy, USER, "administrator")

    assert promoted.status_code == 200
    assert promoted.json() == {"email": USER, "role": "administrator", "privileged_projects": False}
    assert role_of(dockyard, proxy, USER) == "administrator"
    invited = dockyard.post(
        "/api/invitations", json={"email": "friend@example.com"}, headers=proxy.headers(USER)
    )
    assert invited.status_code == 201


def test_a_demoted_administrator_loses_what_only_administrators_can_do(
    dockyard: TestClient, proxy: AccessProxy, add_user: Callable[[str], None]
) -> None:
    add_user(USER)
    change_role(dockyard, proxy, USER, "administrator")

    demoted = change_role(dockyard, proxy, ADMINISTRATOR, "user", by=USER)

    assert demoted.status_code == 200
    assert role_of(dockyard, proxy, ADMINISTRATOR) == "user"
    refused = dockyard.get("/api/users", headers=proxy.headers(ADMINISTRATOR))
    assert refused.status_code == 403


def test_the_last_administrator_cannot_be_demoted(
    dockyard: TestClient, proxy: AccessProxy, add_user: Callable[[str], None]
) -> None:
    add_user(USER)

    refused = change_role(dockyard, proxy, ADMINISTRATOR, "user")

    assert refused.status_code == 409
    assert refused.json()["error"] == "last_administrator"
    assert "last Administrator" in refused.json()["message"]
    assert role_of(dockyard, proxy, ADMINISTRATOR) == "administrator"


def test_an_administrator_can_be_demoted_once_another_one_exists(
    dockyard: TestClient, proxy: AccessProxy, add_user: Callable[[str], None]
) -> None:
    add_user(USER)
    change_role(dockyard, proxy, USER, "administrator")
    change_role(dockyard, proxy, USER, "user")

    refused = change_role(dockyard, proxy, ADMINISTRATOR, "user")

    assert refused.status_code == 409


def test_changing_the_role_of_someone_who_is_not_a_user_is_not_found(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    assert change_role(dockyard, proxy, "nobody@example.com", "administrator").status_code == 404


@pytest.mark.parametrize("role", ["root", "admin", ""])
def test_only_the_two_roles_exist(dockyard: TestClient, proxy: AccessProxy, role: str) -> None:
    refused = change_role(dockyard, proxy, ADMINISTRATOR, role)

    assert refused.status_code == 422
    assert role_of(dockyard, proxy, ADMINISTRATOR) == "administrator"


def remove(dockyard: TestClient, proxy: AccessProxy, email: str, by: str = ADMINISTRATOR) -> Any:
    return dockyard.delete(f"/api/users/{email}", headers=proxy.headers(by))


def test_a_removed_user_is_refused_from_their_next_request(
    dockyard: TestClient, proxy: AccessProxy, add_user: Callable[[str], None]
) -> None:
    add_user(USER)

    removed = remove(dockyard, proxy, USER)

    assert removed.status_code == 204
    refused = dockyard.get("/api/me", headers=proxy.headers(USER))
    assert refused.status_code == 403
    assert refused.json()["error"] == "not_invited"
    users = dockyard.get("/api/users", headers=proxy.headers(ADMINISTRATOR)).json()
    assert [u["email"] for u in users] == [ADMINISTRATOR]


def test_a_removed_user_cannot_open_a_websocket_any_more(
    dockyard: TestClient, proxy: AccessProxy, add_user: Callable[[str], None]
) -> None:
    add_user(USER)
    dockyard.post("/api/claude-account/login", headers=proxy.headers(USER))

    remove(dockyard, proxy, USER)

    with (
        pytest.raises(WebSocketDisconnect) as refused,
        dockyard.websocket_connect(
            "/ws/claude-account/login", headers=proxy.headers(USER)
        ) as terminal,
    ):
        terminal.receive_bytes()
    assert refused.value.code == 4403


def test_removing_a_user_removes_their_projects_git_credentials_and_claude_account(
    dockyard: TestClient,
    proxy: AccessProxy,
    runtime: FakeRuntime,
    add_user: Callable[[str], None],
) -> None:
    add_user(USER)
    theirs = proxy.headers(USER)
    connect_claude_account(dockyard, theirs)
    runtime.publish_repository(REPOSITORY)
    body = {"name": "Theirs", "repository": REPOSITORY, "visibility": "public"}
    project = dockyard.post("/api/projects", json=body, headers=theirs).json()["id"]
    assert wait_until(lambda: runtime.project_status(project) == ProjectStatus.RUNNING)
    credential = {"label": "Theirs", "provider": "github", "token": "ghp_their0token"}
    dockyard.post("/api/git-credentials", json=credential, headers=theirs)

    remove(dockyard, proxy, USER)

    assert runtime.project_status(project) == ProjectStatus.MISSING
    add_user(USER)  # invited again: they start from nothing
    assert dockyard.get("/api/projects", headers=theirs).json() == []
    assert dockyard.get("/api/git-credentials", headers=theirs).json() == []
    assert dockyard.get("/api/claude-account", headers=theirs).json()["connected"] is False


def test_removing_a_user_leaves_other_users_alone(
    dockyard: TestClient,
    proxy: AccessProxy,
    runtime: FakeRuntime,
    add_user: Callable[[str], None],
) -> None:
    mine = proxy.headers(ADMINISTRATOR)
    connect_claude_account(dockyard, mine)
    runtime.publish_repository(REPOSITORY)
    body = {"name": "Mine", "repository": REPOSITORY, "visibility": "public"}
    project = dockyard.post("/api/projects", json=body, headers=mine).json()["id"]
    assert wait_until(lambda: runtime.project_status(project) == ProjectStatus.RUNNING)
    add_user(USER)

    remove(dockyard, proxy, USER)

    assert runtime.project_status(project) == ProjectStatus.RUNNING
    assert dockyard.get("/api/claude-account", headers=mine).json()["connected"] is True


def test_the_last_administrator_cannot_be_removed(
    dockyard: TestClient, proxy: AccessProxy, add_user: Callable[[str], None]
) -> None:
    add_user(USER)
    change_role(dockyard, proxy, USER, "administrator")
    remove(dockyard, proxy, USER)

    refused = remove(dockyard, proxy, ADMINISTRATOR)

    assert refused.status_code == 409
    assert refused.json()["error"] == "last_administrator"
    assert role_of(dockyard, proxy, ADMINISTRATOR) == "administrator"


def test_an_administrator_removes_another_administrator(
    dockyard: TestClient, proxy: AccessProxy, add_user: Callable[[str], None]
) -> None:
    add_user(USER)
    change_role(dockyard, proxy, USER, "administrator")

    assert remove(dockyard, proxy, ADMINISTRATOR, by=USER).status_code == 204
    assert dockyard.get("/api/me", headers=proxy.headers(ADMINISTRATOR)).status_code == 403


def test_removing_someone_who_is_not_a_user_is_not_found(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    assert remove(dockyard, proxy, "nobody@example.com").status_code == 404


def test_nothing_is_removed_while_docker_is_unreachable(
    dockyard: TestClient,
    proxy: AccessProxy,
    runtime: FakeRuntime,
    add_user: Callable[[str], None],
) -> None:
    add_user(USER)

    with runtime.unavailable():
        refused = remove(dockyard, proxy, USER)

    assert refused.status_code == 503
    assert dockyard.get("/api/me", headers=proxy.headers(USER)).status_code == 200
