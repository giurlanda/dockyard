"""Runtime contract: a Project's environment, where its Remote Control Server runs.

The stand-in for Claude Code (tests/runtime/stand-in) reports, when it starts, what it sees from
inside the environment; the in-memory fake plays the same report.
"""

from collections.abc import Callable

import pytest

from dockyard.runtime import CloneState, ProjectMissing, ProjectStatus, Runtime

from .conftest import PUBLIC_REPOSITORY, MakeRuntime, new_project, wait_until

FORBIDDEN = [
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
]


def test_a_started_project_runs_its_remote_control_server_in_its_own_path(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    project = cloned(runtime, user)

    runtime.start_project(user, project, name="Hello World", permission_mode="acceptEdits")

    assert runtime.project_status(project) == ProjectStatus.RUNNING
    log = server_ready(runtime, project)
    assert "remote-control --name=Hello World --spawn worktree --permission-mode acceptEdits" in log
    assert f"in /workspace/{project}" in log


def test_a_stopped_project_runs_nothing_and_keeps_its_code(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    project = cloned(runtime, user)
    runtime.start_project(user, project, name="Hello", permission_mode="default")
    server_ready(runtime, project)

    runtime.stop_project(project)

    assert runtime.project_status(project) == ProjectStatus.STOPPED
    assert runtime.project_log(project) == ""
    runtime.stop_project(project)  # stopping again is harmless


def test_the_users_claude_config_trusts_each_project_path_so_no_prompt_blocks_the_server(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    first, second = cloned(runtime, user), cloned(runtime, user)

    runtime.start_project(user, first, name="First", permission_mode="default")
    server_ready(runtime, first)
    runtime.start_project(user, second, name="Second", permission_mode="default")

    log = server_ready(runtime, second)
    assert "Quick safety check" not in log
    assert f"trusted: {' '.join(sorted([f'/workspace/{first}', f'/workspace/{second}']))}" in log
    assert "claude config mode: 600" in log


def test_two_projects_of_the_same_user_get_their_own_remote_control_environment(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    first, second = cloned(runtime, user), cloned(runtime, user)

    runtime.start_project(user, first, name="First", permission_mode="default")
    runtime.start_project(user, second, name="Second", permission_mode="default")

    assert reported(server_ready(runtime, first), "environment") != reported(
        server_ready(runtime, second), "environment"
    )


def test_code_home_and_remote_control_environment_stay_across_stop_and_start(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    project = cloned(runtime, user)
    runtime.start_project(user, project, name="Hello", permission_mode="default")
    first = server_ready(runtime, project)

    runtime.stop_project(project)
    runtime.start_project(user, project, name="Hello", permission_mode="default")

    again = server_ready(runtime, project)
    assert reported(first, "starts") == "1"
    assert reported(again, "starts") == "2"  # counted in the home
    assert reported(again, "code") == "Merge pull request #6 from Spaceghost/patch-1"
    assert reported(again, "environment") == reported(first, "environment")


def test_starting_a_running_project_restarts_it_in_a_new_environment(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    project = cloned(runtime, user)
    runtime.start_project(user, project, name="Hello", permission_mode="default")
    server_ready(runtime, project)

    runtime.start_project(user, project, name="Renamed", permission_mode="auto")

    log = server_ready(runtime, project)
    assert "--name=Renamed --spawn worktree --permission-mode auto" in log
    assert reported(log, "starts") == "2"


def test_a_project_without_code_cannot_start(runtime: Runtime, new_user: Callable[[], str]) -> None:
    user = new_user()
    project = cloned(runtime, user)
    runtime.remove_project(project, code=True)

    with pytest.raises(ProjectMissing):
        runtime.start_project(user, project, name="Hello", permission_mode="default")
    assert runtime.project_status(project) == ProjectStatus.MISSING


def test_a_project_runs_hardened_as_the_non_root_user_with_limits(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    project = cloned(runtime, user)

    runtime.start_project(user, project, name="Hello", permission_mode="bypassPermissions")

    log = server_ready(runtime, project)
    assert reported(log, "user") == "1000"
    assert reported(log, "capabilities") == "0000000000000000"
    assert reported(log, "no new privileges") == "1"
    assert reported(log, "pids limit") == "2048"
    assert reported(log, "memory limit") == str(4 * 1024**3)
    assert reported(log, "cpu limit") == "200000 100000"  # 2 CPUs


def test_a_project_runs_within_the_cpu_and_ram_it_was_given(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    project = cloned(runtime, user)

    runtime.start_project(
        user, project, name="Hello", permission_mode="default", cpus=1.5, memory=1024**3
    )

    log = server_ready(runtime, project)
    assert reported(log, "memory limit") == str(1024**3)
    assert reported(log, "cpu limit") == "150000 100000"  # 1.5 CPUs
    stats = runtime.project_stats(project)
    assert stats is not None
    assert (stats.cpus, stats.memory_limit) == (1.5, 1024**3)


def test_a_project_resolves_names_with_the_configured_dns_servers(
    make_runtime: MakeRuntime, new_user: Callable[[], str]
) -> None:
    runtime = make_runtime(dns=["9.9.9.9", "8.8.8.8"])
    user = new_user()
    project = cloned(runtime, user)

    runtime.start_project(user, project, name="Hello", permission_mode="default")

    assert reported(server_ready(runtime, project), "dns") == "9.9.9.9 8.8.8.8"


def test_a_project_never_receives_settings_that_disable_remote_control(
    runtime: Runtime, new_user: Callable[[], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in FORBIDDEN:  # even when Dockyard itself has them
        monkeypatch.setenv(name, "set-in-dockyard")
    user = new_user()
    project = cloned(runtime, user)

    runtime.start_project(user, project, name="Hello", permission_mode="default")

    assert reported(server_ready(runtime, project), "anthropic environment") == ""


def reported(log: str, fact: str) -> str:
    """What the stand-in reported about `fact` when it started."""
    for line in log.splitlines():
        if line.startswith(f"{fact}: "):
            return line.removeprefix(f"{fact}: ").strip()
    raise AssertionError(f"The server never reported its {fact}: {log!r}")


def cloned(runtime: Runtime, user: str) -> str:
    """A new Project of the User, once its code is there."""
    project = new_project()
    runtime.start_clone(user, project, PUBLIC_REPOSITORY)
    assert wait_until(lambda: _clone_ended(runtime, project), timeout=120)
    runtime.finish_clone(project)
    return project


def _clone_ended(runtime: Runtime, project: str) -> bool:
    clone = runtime.clone(project)
    return clone is not None and clone.state != CloneState.RUNNING


def server_ready(runtime: Runtime, project: str) -> str:
    """What the Project's server printed, once it is ready for Sessions."""
    assert wait_until(lambda: "Remote Control ready" in runtime.project_log(project)), (
        runtime.project_log(project)
    )
    return runtime.project_log(project)
