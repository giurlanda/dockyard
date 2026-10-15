"""A User's Projects run their Remote Control Server: started, stopped, restarted, or waiting
for the User's Claude Account."""

import time
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, AccessProxy, connect_claude_account, wait_until

REPOSITORY = "https://github.com/octocat/Hello-World"
OTHER_USER = "another.user@example.com"


@pytest.fixture(autouse=True)
def public_repository(runtime: FakeRuntime) -> None:
    runtime.publish_repository(REPOSITORY)


@pytest.fixture
def signed_in(proxy: AccessProxy) -> dict[str, str]:
    return proxy.headers(ADMINISTRATOR)


def create(dockyard: TestClient, headers: dict[str, str], **fields: str) -> Any:
    body = {"name": "Hello", "repository": REPOSITORY, "visibility": "public", **fields}
    created = dockyard.post("/api/projects", json=body, headers=headers)
    assert created.status_code == 202, created.text
    return created.json()


def status_of(dockyard: TestClient, headers: dict[str, str], project: str) -> str:
    found = dockyard.get(f"/api/projects/{project}", headers=headers)
    assert found.status_code == 200
    status: str = found.json()["status"]
    return status


def becomes(dockyard: TestClient, headers: dict[str, str], project: str, status: str) -> bool:
    return wait_until(lambda: status_of(dockyard, headers, project) == status)


def act(dockyard: TestClient, headers: dict[str, str], project: str, action: str) -> Any:
    return dockyard.post(f"/api/projects/{project}/{action}", headers=headers)


def test_a_project_created_before_the_claude_account_is_connected_is_cloned_and_waits(
    dockyard: TestClient, signed_in: dict[str, str], runtime: FakeRuntime
) -> None:
    project = create(dockyard, signed_in)["id"]

    assert becomes(dockyard, signed_in, project, "waiting")
    assert runtime.project_log(project) == ""


def test_waiting_projects_start_once_the_claude_account_is_connected(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    first = create(dockyard, signed_in, name="First")["id"]
    second = create(dockyard, signed_in, name="Second")["id"]
    assert becomes(dockyard, signed_in, first, "waiting")
    assert becomes(dockyard, signed_in, second, "waiting")

    connect_claude_account(dockyard, signed_in)

    assert becomes(dockyard, signed_in, first, "running")
    assert becomes(dockyard, signed_in, second, "running")


def test_a_project_of_a_connected_user_runs_its_server_named_after_it_once_cloned(
    dockyard: TestClient, signed_in: dict[str, str], runtime: FakeRuntime
) -> None:
    connect_claude_account(dockyard, signed_in)

    project = create(dockyard, signed_in, name="Hello World")["id"]

    assert becomes(dockyard, signed_in, project, "running")
    log = runtime.project_log(project)
    assert "--name=Hello World --spawn worktree --permission-mode default" in log
    assert f"in /workspace/{project}" in log


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "auto", "bypassPermissions"])
def test_the_permission_mode_is_chosen_at_creation(
    dockyard: TestClient, signed_in: dict[str, str], runtime: FakeRuntime, mode: str
) -> None:
    connect_claude_account(dockyard, signed_in)

    project = create(dockyard, signed_in, permission_mode=mode)

    assert project["permission_mode"] == mode
    assert becomes(dockyard, signed_in, project["id"], "running")
    assert f"--permission-mode {mode}\r\n" in runtime.project_log(project["id"])


@pytest.mark.parametrize("mode", ["dontAsk", "plan", "manual", "", "bypassPermissions --x"])
def test_other_permission_modes_are_refused(
    dockyard: TestClient, signed_in: dict[str, str], mode: str
) -> None:
    body = {"name": "Hello", "repository": REPOSITORY, "visibility": "public"}

    refused = dockyard.post(
        "/api/projects", json={**body, "permission_mode": mode}, headers=signed_in
    )

    assert refused.status_code == 422
    assert dockyard.get("/api/projects", headers=signed_in).json() == []


def test_a_user_stops_starts_and_restarts_a_project(
    dockyard: TestClient, signed_in: dict[str, str], runtime: FakeRuntime
) -> None:
    connect_claude_account(dockyard, signed_in)
    project = create(dockyard, signed_in)["id"]
    assert becomes(dockyard, signed_in, project, "running")

    stopped = act(dockyard, signed_in, project, "stop")
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "stopped"
    assert runtime.project_log(project) == ""

    started = act(dockyard, signed_in, project, "start")
    assert started.json()["status"] == "running"
    assert "starts: 2" in runtime.project_log(project)

    restarted = act(dockyard, signed_in, project, "restart")
    assert restarted.json()["status"] == "running"
    assert "starts: 3" in runtime.project_log(project)


def test_a_stopped_project_stays_stopped_when_the_claude_account_is_connected(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    project = create(dockyard, signed_in)["id"]
    assert becomes(dockyard, signed_in, project, "waiting")

    assert act(dockyard, signed_in, project, "stop").json()["status"] == "stopped"
    connect_claude_account(dockyard, signed_in)

    assert status_of(dockyard, signed_in, project) == "stopped"
    assert act(dockyard, signed_in, project, "start").json()["status"] == "running"


def test_starting_a_project_without_a_claude_account_makes_it_wait(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    project = create(dockyard, signed_in)["id"]
    assert becomes(dockyard, signed_in, project, "waiting")
    act(dockyard, signed_in, project, "stop")

    assert act(dockyard, signed_in, project, "start").json()["status"] == "waiting"
    connect_claude_account(dockyard, signed_in)
    assert becomes(dockyard, signed_in, project, "running")


def test_a_project_cannot_be_started_while_it_clones(
    dockyard: TestClient, signed_in: dict[str, str], runtime: FakeRuntime
) -> None:
    with runtime.clones_paused():
        project = create(dockyard, signed_in)["id"]

        refused = act(dockyard, signed_in, project, "start")

    assert refused.status_code == 409
    assert refused.json()["error"] == "cloning"
    assert "clon" in refused.json()["message"]


def test_a_project_whose_server_keeps_failing_shows_an_error_until_restarted(
    dockyard: TestClient, signed_in: dict[str, str], runtime: FakeRuntime
) -> None:
    connect_claude_account(dockyard, signed_in)
    project = create(dockyard, signed_in)["id"]
    assert becomes(dockyard, signed_in, project, "running")

    runtime.crash(project)

    assert status_of(dockyard, signed_in, project) == "error"
    assert act(dockyard, signed_in, project, "restart").json()["status"] == "running"


def test_a_user_cannot_start_stop_or_restart_another_users_project(
    dockyard: TestClient,
    signed_in: dict[str, str],
    proxy: AccessProxy,
    add_user: Callable[[str], None],
) -> None:
    add_user(OTHER_USER)
    connect_claude_account(dockyard, signed_in)
    project = create(dockyard, signed_in)["id"]
    assert becomes(dockyard, signed_in, project, "running")
    other = proxy.headers(OTHER_USER)

    for action in ("start", "stop", "restart"):
        assert act(dockyard, other, project, action).status_code == 404
    assert status_of(dockyard, signed_in, project) == "running"


def test_a_project_is_not_started_when_dockyard_cannot_reach_docker(
    dockyard: TestClient, signed_in: dict[str, str], runtime: FakeRuntime
) -> None:
    connect_claude_account(dockyard, signed_in)
    project = create(dockyard, signed_in)["id"]
    assert becomes(dockyard, signed_in, project, "running")
    act(dockyard, signed_in, project, "stop")

    with runtime.unavailable():
        refused = act(dockyard, signed_in, project, "start")

    assert refused.status_code == 503
    assert refused.json()["error"] == "runtime_unavailable"
    assert status_of(dockyard, signed_in, project) == "stopped"


def test_waiting_projects_start_when_a_sign_in_begun_before_dockyard_restarted_completes(
    start_dockyard: Callable[..., TestClient], signed_in: dict[str, str]
) -> None:
    with start_dockyard() as before:
        project = create(before, signed_in)["id"]
        assert becomes(before, signed_in, project, "waiting")
        before.post("/api/claude-account/login", headers=signed_in)

    after = start_dockyard()
    connect_claude_account(after, signed_in, start=False)

    assert becomes(after, signed_in, project, "running")


def test_waiting_projects_start_even_if_docker_was_unreachable_during_the_sign_in(
    dockyard: TestClient, signed_in: dict[str, str], runtime: FakeRuntime
) -> None:
    project = create(dockyard, signed_in)["id"]
    assert becomes(dockyard, signed_in, project, "waiting")
    dockyard.post("/api/claude-account/login", headers=signed_in)

    with runtime.unavailable():
        time.sleep(1.5)  # longer than Dockyard waits between two looks at the sign-in
    connect_claude_account(dockyard, signed_in, start=False)

    assert becomes(dockyard, signed_in, project, "running")
