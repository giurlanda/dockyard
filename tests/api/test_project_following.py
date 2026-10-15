"""A User follows a running Project: its live log, its CPU and RAM, and its console, where they
answer what its Remote Control Server asks. Only the Project's owner reaches any of them."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

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


def running_project(dockyard: TestClient, headers: dict[str, str]) -> str:
    """A new Project of the signed-in User, once its Remote Control Server runs."""
    connect_claude_account(dockyard, headers)
    body = {"name": "Hello", "repository": REPOSITORY, "visibility": "public"}
    project: str = dockyard.post("/api/projects", json=body, headers=headers).json()["id"]
    assert wait_until(lambda: status_of(dockyard, headers, project) == "running")
    return project


def status_of(dockyard: TestClient, headers: dict[str, str], project: str) -> str:
    status: str = dockyard.get(f"/api/projects/{project}", headers=headers).json()["status"]
    return status


def receive_until(socket: WebSocketTestSession, text: str) -> str:
    shown = ""
    while text not in shown:
        shown += socket.receive_bytes().decode(errors="replace")
    return shown


def closed_with(dockyard: TestClient, path: str, headers: dict[str, str]) -> int:
    """How Dockyard closes the socket at `path` before sending anything."""
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        dockyard.websocket_connect(path, headers=headers) as socket,
    ):
        socket.receive_bytes()
    return closed.value.code


def test_the_log_shows_what_the_server_printed_and_ends_when_the_project_stops(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    project = running_project(dockyard, signed_in)

    with dockyard.websocket_connect(f"/ws/projects/{project}/logs", headers=signed_in) as log:
        assert "--name=Hello --spawn worktree" in receive_until(log, "Remote Control ready")
        dockyard.post(f"/api/projects/{project}/stop", headers=signed_in)
        with pytest.raises(WebSocketDisconnect) as ended:
            receive_until(log, "the server never prints this")

    assert ended.value.code == 1000


def test_leaving_the_log_stops_following_it(
    dockyard: TestClient, signed_in: dict[str, str], runtime: FakeRuntime
) -> None:
    project = running_project(dockyard, signed_in)

    with dockyard.websocket_connect(f"/ws/projects/{project}/logs", headers=signed_in) as log:
        receive_until(log, "Remote Control ready")
        assert runtime.watching(project) == 1

    assert wait_until(lambda: runtime.watching(project) == 0)
    assert status_of(dockyard, signed_in, project) == "running"


def test_typing_in_the_log_does_nothing(
    dockyard: TestClient, signed_in: dict[str, str], runtime: FakeRuntime
) -> None:
    project = running_project(dockyard, signed_in)

    with dockyard.websocket_connect(f"/ws/projects/{project}/logs", headers=signed_in) as log:
        receive_until(log, "Remote Control ready")
        log.send_json({"type": "input", "data": "\x03"})
        log.send_json({"type": "resize", "cols": 100, "rows": 30})

    assert wait_until(lambda: runtime.watching(project) == 0)
    log_text = runtime.project_log(project)
    assert "starts: 1" in log_text
    assert "^C" not in log_text
    assert "Terminal size" not in log_text


def test_the_stats_show_the_cpu_and_ram_a_project_uses_now(
    dockyard: TestClient, signed_in: dict[str, str], runtime: FakeRuntime
) -> None:
    project = running_project(dockyard, signed_in)
    runtime.use(project, cpu=12.5, memory=300 * 1024**2)

    first = dockyard.get(f"/api/projects/{project}/stats", headers=signed_in)
    runtime.use(project, cpu=150.0, memory=1024**3)
    then = dockyard.get(f"/api/projects/{project}/stats", headers=signed_in)

    assert first.status_code == 200
    assert first.json() == {
        "cpu": 12.5,
        "cpus": 2,
        "memory": 300 * 1024**2,
        "memory_limit": 4 * 1024**3,
    }
    assert then.json()["cpu"] == 150.0
    assert then.json()["memory"] == 1024**3


def test_a_stopped_project_has_no_stats(dockyard: TestClient, signed_in: dict[str, str]) -> None:
    project = running_project(dockyard, signed_in)
    dockyard.post(f"/api/projects/{project}/stop", headers=signed_in)

    stats = dockyard.get(f"/api/projects/{project}/stats", headers=signed_in)

    assert stats.status_code == 409
    assert stats.json()["error"] == "not_running"
    assert "not running" in stats.json()["message"]


def test_the_console_is_the_servers_terminal_and_follows_its_size(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    project = running_project(dockyard, signed_in)

    with dockyard.websocket_connect(
        f"/ws/projects/{project}/console", headers=signed_in
    ) as console:
        receive_until(console, "Remote Control ready")
        console.send_json({"type": "resize", "cols": 100, "rows": 30})

        receive_until(console, "Terminal size: 30 100")


def test_ctrl_c_in_the_console_stops_the_server_which_starts_again(
    dockyard: TestClient, signed_in: dict[str, str], runtime: FakeRuntime
) -> None:
    project = running_project(dockyard, signed_in)

    with dockyard.websocket_connect(
        f"/ws/projects/{project}/console", headers=signed_in
    ) as console:
        receive_until(console, "Remote Control ready")
        console.send_json({"type": "input", "data": "\x03"})
        with pytest.raises(WebSocketDisconnect) as ended:
            receive_until(console, "the server never prints this")

    assert ended.value.code == 1000
    assert wait_until(lambda: "starts: 2" in runtime.project_log(project))
    assert status_of(dockyard, signed_in, project) == "running"


def test_a_project_whose_server_shows_the_trust_prompt_asks_to_open_the_console(
    dockyard: TestClient, signed_in: dict[str, str], runtime: FakeRuntime
) -> None:
    project = running_project(dockyard, signed_in)

    runtime.lose_trust(project)

    assert status_of(dockyard, signed_in, project) == "trust_prompt"
    with dockyard.websocket_connect(
        f"/ws/projects/{project}/console", headers=signed_in
    ) as console:
        receive_until(console, "Quick safety check")
        console.send_json({"type": "input", "data": "1\r"})
        receive_until(console, "Remote Control ready")
    assert status_of(dockyard, signed_in, project) == "running"


@pytest.mark.parametrize("stream", ["logs", "console"])
def test_a_stopped_project_has_no_log_or_console(
    dockyard: TestClient, signed_in: dict[str, str], stream: str
) -> None:
    project = running_project(dockyard, signed_in)
    dockyard.post(f"/api/projects/{project}/stop", headers=signed_in)

    assert closed_with(dockyard, f"/ws/projects/{project}/{stream}", signed_in) == 4409


@pytest.mark.parametrize("stream", ["logs", "console"])
def test_nobody_but_its_owner_opens_a_projects_log_or_console_not_even_an_administrator(
    dockyard: TestClient,
    proxy: AccessProxy,
    add_user: Callable[[str], None],
    stream: str,
) -> None:
    add_user(OTHER_USER)
    owner = proxy.headers(OTHER_USER)  # not an Administrator
    project = running_project(dockyard, owner)

    administrator = proxy.headers(ADMINISTRATOR)
    assert closed_with(dockyard, f"/ws/projects/{project}/{stream}", administrator) == 4404
    assert dockyard.get(f"/api/projects/{project}/stats", headers=administrator).status_code == 404
    assert dockyard.get(f"/projects/{project}/{stream}", headers=administrator).status_code == 404


@pytest.mark.parametrize("stream", ["logs", "console"])
def test_nobody_reaches_a_log_or_console_without_going_through_the_proxy(
    dockyard: TestClient, signed_in: dict[str, str], stream: str
) -> None:
    project = running_project(dockyard, signed_in)

    assert closed_with(dockyard, f"/ws/projects/{project}/{stream}", {}) == 4401


def test_users_are_told_when_dockyard_cannot_reach_docker(
    dockyard: TestClient, signed_in: dict[str, str], runtime: FakeRuntime
) -> None:
    project = running_project(dockyard, signed_in)

    with runtime.unavailable():
        for stream in ("logs", "console"):
            assert closed_with(dockyard, f"/ws/projects/{project}/{stream}", signed_in) == 4503
        stats = dockyard.get(f"/api/projects/{project}/stats", headers=signed_in)

    assert stats.status_code == 503
