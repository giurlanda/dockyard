"""Runtime contract: following a Project while it runs, through its log, its console (the TTY of
its Remote Control Server) and its CPU and RAM usage."""

from collections.abc import Callable

import pytest

from dockyard.runtime import ProjectNotRunning, ProjectStatus, Runtime

from .conftest import LoseTrust, Screen, wait_until
from .test_project_environment import cloned, server_ready


def running(runtime: Runtime, user: str) -> str:
    """A new Project of the User, its Remote Control Server ready."""
    project = cloned(runtime, user)
    runtime.start_project(user, project, name="Hello", permission_mode="default")
    server_ready(runtime, project)
    return project


def test_the_log_shows_what_the_server_printed_then_follows_it_until_it_stops(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    project = running(runtime, user)

    log = Screen(runtime.follow_project_log(project))

    log.wait_for(f"in /workspace/{project}")
    log.wait_for("Remote Control ready")
    runtime.stop_project(project)
    log.wait_until_ended()


def test_letting_go_of_the_log_ends_it_at_once(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    project = running(runtime, new_user())
    log = Screen(runtime.follow_project_log(project))
    log.wait_for("Remote Control ready")

    log.tty.close()

    log.wait_until_ended(timeout=5)


def test_a_stopped_project_has_no_log_to_follow(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    project = running(runtime, user)
    runtime.stop_project(project)

    with pytest.raises(ProjectNotRunning):
        runtime.follow_project_log(project)


def test_the_console_shows_what_the_server_printed_and_passes_on_its_terminal_size(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    project = running(runtime, new_user())

    console = Screen(runtime.attach_project(project))
    console.wait_for("Remote Control ready")
    console.tty.resize(rows=30, cols=100)

    console.wait_for("Terminal size: 30 100")


def test_ctrl_c_in_the_console_stops_the_server_which_then_starts_again(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    project = running(runtime, new_user())
    console = Screen(runtime.attach_project(project))
    console.wait_for("Remote Control ready")

    console.type("\x03")

    console.wait_until_ended()
    assert wait_until(lambda: "starts: 2" in runtime.project_log(project), timeout=30)


def test_leaving_the_console_keeps_the_server_running(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    project = running(runtime, new_user())
    console = Screen(runtime.attach_project(project))
    console.wait_for("Remote Control ready")

    console.tty.close()

    console.wait_until_ended(timeout=5)
    assert runtime.project_status(project) == ProjectStatus.RUNNING
    again = Screen(runtime.attach_project(project))
    again.wait_for("Remote Control ready")


def test_a_stopped_project_has_no_console(runtime: Runtime, new_user: Callable[[], str]) -> None:
    project = running(runtime, new_user())
    runtime.stop_project(project)

    with pytest.raises(ProjectNotRunning):
        runtime.attach_project(project)


def test_a_running_project_reports_its_cpu_and_memory_within_its_limits(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    project = running(runtime, new_user())

    stats = runtime.project_stats(project)

    assert stats is not None
    assert stats.cpus == 2
    assert stats.memory_limit == 4 * 1024**3
    assert 0 < stats.memory < stats.memory_limit
    assert 0 <= stats.cpu <= 200


def test_a_stopped_project_reports_no_usage(runtime: Runtime, new_user: Callable[[], str]) -> None:
    project = running(runtime, new_user())
    runtime.stop_project(project)

    assert runtime.project_stats(project) is None


def test_a_server_that_lost_its_trust_asks_for_it_in_the_console_until_answered(
    runtime: Runtime, new_user: Callable[[], str], lose_trust: LoseTrust
) -> None:
    project = running(runtime, new_user())

    lose_trust(runtime, project)

    assert wait_until(lambda: runtime.project_status(project) == ProjectStatus.TRUST_PROMPT)
    console = Screen(runtime.attach_project(project))
    console.wait_for("Quick safety check")
    console.type("1\r")
    console.wait_for("Remote Control ready")
    assert runtime.project_status(project) == ProjectStatus.RUNNING


def test_a_server_whose_trust_is_refused_keeps_asking(
    runtime: Runtime, new_user: Callable[[], str], lose_trust: LoseTrust
) -> None:
    project = running(runtime, new_user())
    lose_trust(runtime, project)
    assert wait_until(lambda: runtime.project_status(project) == ProjectStatus.TRUST_PROMPT)
    console = Screen(runtime.attach_project(project))
    console.wait_for("Quick safety check")

    console.type("2\r")

    console.wait_until_ended()
    assert wait_until(lambda: _asks_again(runtime, project), timeout=30)


def _asks_again(runtime: Runtime, project: str) -> bool:
    """Its server restarted and asks again."""
    if runtime.project_status(project) != ProjectStatus.TRUST_PROMPT:
        return False
    try:
        console = Screen(runtime.attach_project(project))
    except ProjectNotRunning:
        return False
    try:
        console.wait_for("Quick safety check", timeout=2)
        return True
    except AssertionError:
        return False
    finally:
        console.tty.close()
