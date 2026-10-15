"""A User opens a Terminal in their running Project: a shell of the non-root user in the Project's
path, where they run git, gh, glab or Claude Code. Only the Project's owner opens it."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, AccessProxy, wait_until
from .test_project_following import receive_until, running_project, status_of


@pytest.fixture(autouse=True)
def public_repository(runtime: FakeRuntime) -> None:
    runtime.publish_repository("https://github.com/octocat/Hello-World")


@pytest.fixture
def signed_in(proxy: AccessProxy) -> dict[str, str]:
    return proxy.headers(ADMINISTRATOR)


def test_the_terminal_is_a_shell_of_the_non_root_user_in_the_project_that_follows_its_size(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    project = running_project(dockyard, signed_in)

    with dockyard.websocket_connect(
        f"/ws/projects/{project}/terminal", headers=signed_in
    ) as terminal:
        terminal.send_json({"type": "resize", "cols": 100, "rows": 30})
        terminal.send_json({"type": "input", "data": "pwd\r"})
        receive_until(terminal, f"/workspace/{project}\r\n")
        terminal.send_json({"type": "input", "data": "id -u\r"})
        receive_until(terminal, "1000\r\n")
        terminal.send_json({"type": "input", "data": "stty size\r"})
        receive_until(terminal, "30 100\r\n")


def test_leaving_the_terminal_ends_its_shell_and_what_it_runs(
    dockyard: TestClient, signed_in: dict[str, str], runtime: FakeRuntime
) -> None:
    project = running_project(dockyard, signed_in)

    with dockyard.websocket_connect(
        f"/ws/projects/{project}/terminal", headers=signed_in
    ) as terminal:
        receive_until(terminal, "$ ")
        terminal.send_json({"type": "input", "data": "sleep 600\r"})
        assert wait_until(lambda: runtime.terminal_processes(project) == 2)

    assert wait_until(lambda: runtime.terminal_processes(project) == 0)
    assert status_of(dockyard, signed_in, project) == "running"


def test_exiting_the_shell_closes_the_terminal_and_the_project_keeps_running(
    dockyard: TestClient, signed_in: dict[str, str], runtime: FakeRuntime
) -> None:
    project = running_project(dockyard, signed_in)

    with dockyard.websocket_connect(
        f"/ws/projects/{project}/terminal", headers=signed_in
    ) as terminal:
        receive_until(terminal, "$ ")
        terminal.send_json({"type": "input", "data": "\x03exit\r"})
        with pytest.raises(WebSocketDisconnect) as ended:
            receive_until(terminal, "the shell never prints this")

    assert ended.value.code == 1000
    assert status_of(dockyard, signed_in, project) == "running"
    assert "starts: 2" not in runtime.project_log(project)


def test_a_terminal_opened_by_another_site_is_refused(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    """Only Dockyard's own pages act for a User (ADR 0003): another site would type in the shell."""
    project = running_project(dockyard, signed_in)
    another_site = {**signed_in, "Origin": "https://another-site.example"}

    with (
        pytest.raises(WebSocketDisconnect) as refused,
        dockyard.websocket_connect(
            f"/ws/projects/{project}/terminal", headers=another_site
        ) as terminal,
    ):
        terminal.receive_bytes()

    assert refused.value.code == 4403
