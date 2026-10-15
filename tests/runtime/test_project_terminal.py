"""Runtime contract: a Project's Terminal, an interactive shell in its running environment where
its User runs git, gh, glab or Claude Code."""

from collections.abc import Callable

import pytest

from dockyard.runtime import ProjectNotRunning, ProjectStatus, Runtime

from .conftest import Screen, TerminalProcesses, wait_until
from .test_project_following import running


def test_the_terminal_is_a_shell_of_the_non_root_user_in_the_projects_path(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    project = running(runtime, new_user())

    terminal = Screen(runtime.open_terminal(project))
    terminal.type("pwd\r")
    terminal.wait_for(f"/workspace/{project}\r\n")
    terminal.type("id -u\r")

    terminal.wait_for("1000\r\n")


def test_the_terminal_follows_its_size(runtime: Runtime, new_user: Callable[[], str]) -> None:
    project = running(runtime, new_user())
    terminal = Screen(runtime.open_terminal(project))
    terminal.wait_for("$ ")

    terminal.tty.resize(rows=30, cols=100)
    terminal.type("stty size\r")

    terminal.wait_for("30 100\r\n")


def test_exiting_the_shell_ends_the_terminal_and_nothing_else(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    project = running(runtime, new_user())
    terminal = Screen(runtime.open_terminal(project))
    terminal.wait_for("$ ")

    terminal.type("exit\r")

    terminal.wait_until_ended()
    assert runtime.project_status(project) == ProjectStatus.RUNNING
    assert "starts: 2" not in runtime.project_log(project)


def test_the_terminal_ends_when_the_project_stops(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    project = running(runtime, new_user())
    terminal = Screen(runtime.open_terminal(project))
    terminal.wait_for("$ ")

    runtime.stop_project(project)

    terminal.wait_until_ended()


def test_a_stopped_project_has_no_terminal(runtime: Runtime, new_user: Callable[[], str]) -> None:
    project = running(runtime, new_user())
    runtime.stop_project(project)

    with pytest.raises(ProjectNotRunning):
        runtime.open_terminal(project)


def test_ctrl_c_in_the_terminal_interrupts_what_the_shell_runs_never_the_server(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    project = running(runtime, new_user())
    terminal = Screen(runtime.open_terminal(project))
    terminal.wait_for("$ ")
    terminal.type("sleep 600\r")

    terminal.type("\x03")
    terminal.wait_for("^C")
    terminal.type("pwd\r")

    terminal.wait_for(f"/workspace/{project}\r\n")
    assert runtime.project_status(project) == ProjectStatus.RUNNING
    assert "starts: 2" not in runtime.project_log(project)


def test_letting_go_of_the_terminal_ends_its_shell_and_what_it_runs(
    runtime: Runtime, new_user: Callable[[], str], terminal_processes: TerminalProcesses
) -> None:
    project = running(runtime, new_user())
    terminal = Screen(runtime.open_terminal(project))
    terminal.wait_for("$ ")
    terminal.type("sleep 600\r")
    assert wait_until(lambda: terminal_processes(runtime, project) == 2)

    terminal.tty.close()

    terminal.wait_until_ended(timeout=5)
    assert wait_until(lambda: terminal_processes(runtime, project) == 0)
    assert runtime.project_status(project) == ProjectStatus.RUNNING


def test_the_terminal_ends_when_the_server_restarts(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    project = running(runtime, new_user())
    terminal = Screen(runtime.open_terminal(project))
    terminal.wait_for("$ ")
    console = Screen(runtime.attach_project(project))
    console.wait_for("Remote Control ready")

    console.type("\x03")  # stops the server, which starts again

    terminal.wait_until_ended()
    assert wait_until(lambda: "starts: 2" in runtime.project_log(project), timeout=30)


def test_a_terminal_let_go_of_at_once_ends_too(
    runtime: Runtime, new_user: Callable[[], str], terminal_processes: TerminalProcesses
) -> None:
    project = running(runtime, new_user())

    runtime.open_terminal(project).close()

    assert wait_until(lambda: terminal_processes(runtime, project) == 0)
    assert runtime.project_status(project) == ProjectStatus.RUNNING
