"""Runtime contract: a Project's Git Credential, for its clone and for gh, glab and git inside it.

What the tools inside the environment see is read through a Terminal, as a User would.
"""

from collections.abc import Callable

import pytest

from dockyard.runtime import CloneState, GitToken, ProjectNotRunning, Runtime

from .conftest import MISSING_REPOSITORY, PUBLIC_REPOSITORY, Screen, new_project, wait_until
from .test_clone import finished
from .test_project_environment import cloned, reported, server_ready

GITHUB_TOKEN = "github_pat_11DOCKYARD0contract0test0token"
GITLAB_TOKEN = "glpat-dockyard0contract0test"
TOKENS = {"github.com": GITHUB_TOKEN, "gitlab.com": GITLAB_TOKEN}
# What a User types in a Terminal to see the token each tool works with.
TOOLS = {
    "github.com": "gh auth token --hostname github.com",
    "gitlab.com": "glab config get token --host gitlab.com",
}
FILL = "printf 'protocol=https\\nhost={host}\\n\\n' | GIT_TERMINAL_PROMPT=0 git credential fill"
NO_GH_TOKEN = "no oauth token found for github.com"

ExitByItself = Callable[[Runtime, str], None]


def test_a_repository_is_cloned_with_a_token_its_remote_does_not_keep(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    project = new_project()

    runtime.start_clone(
        user, project, PUBLIC_REPOSITORY, credential=GitToken("github.com", GITHUB_TOKEN)
    )

    clone = finished(runtime, project)
    assert clone.state == CloneState.SUCCEEDED, clone.output
    assert GITHUB_TOKEN not in clone.output
    runtime.finish_clone(project)
    runtime.start_project(user, project, name="Hello", permission_mode="default")
    assert reported(server_ready(runtime, project), "remote") == PUBLIC_REPOSITORY


def test_a_token_the_provider_refuses_makes_the_clone_fail_with_gits_error(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    project = new_project()

    runtime.start_clone(
        new_user(), project, MISSING_REPOSITORY, credential=GitToken("github.com", GITHUB_TOKEN)
    )

    clone = finished(runtime, project)
    assert clone.state == CloneState.FAILED
    assert "Authentication failed" in clone.output
    assert GITHUB_TOKEN not in clone.output


@pytest.mark.parametrize("host", ["github.com", "gitlab.com"])
def test_gh_glab_and_git_work_with_the_git_credential_written_into_a_running_project(
    runtime: Runtime, new_user: Callable[[], str], host: str
) -> None:
    project = running(runtime, new_user())

    runtime.write_git_credential(project, GitToken(host, TOKENS[host]))

    typed(runtime, project, TOOLS[host], until=f"{TOKENS[host]}\r\n")
    typed(runtime, project, FILL.format(host=host), until=f"password={TOKENS[host]}")


def test_writing_the_git_credential_again_replaces_it(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    project = running(runtime, new_user())
    runtime.write_git_credential(project, GitToken("github.com", "github_pat_11the0old0token"))

    runtime.write_git_credential(project, GitToken("github.com", GITHUB_TOKEN))

    shown = typed(
        runtime, project, FILL.format(host="github.com"), until=f"password={GITHUB_TOKEN}"
    )
    assert "the0old0token" not in shown


def test_a_restarted_project_holds_no_git_credential_until_it_is_written_again(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    project = running(runtime, user)
    runtime.write_git_credential(project, GitToken("github.com", GITHUB_TOKEN))
    before = runtime.project_starts()[project]

    runtime.start_project(user, project, name="Hello", permission_mode="default")

    assert runtime.project_starts()[project] != before
    assert GITHUB_TOKEN not in typed(runtime, project, TOOLS["github.com"], until=NO_GH_TOKEN)


def test_a_server_restarted_by_itself_is_a_new_start_without_the_git_credential(
    runtime: Runtime, new_user: Callable[[], str], exit_by_itself: ExitByItself
) -> None:
    project = running(runtime, new_user())
    runtime.write_git_credential(project, GitToken("github.com", GITHUB_TOKEN))
    before = runtime.project_starts()[project]

    exit_by_itself(runtime, project)

    assert wait_until(lambda: runtime.project_starts().get(project, before) != before, 30)
    assert wait_until(lambda: "starts: 2" in runtime.project_log(project), timeout=30)
    assert GITHUB_TOKEN not in typed(runtime, project, TOOLS["github.com"], until=NO_GH_TOKEN)


def test_only_running_projects_have_a_start(runtime: Runtime, new_user: Callable[[], str]) -> None:
    user = new_user()
    project = running(runtime, user)
    stopped = cloned(runtime, user)
    assert project in runtime.project_starts()

    runtime.stop_project(project)

    assert project not in runtime.project_starts()
    assert stopped not in runtime.project_starts()


def test_a_git_credential_cannot_be_written_into_a_stopped_project(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    project = cloned(runtime, new_user())

    with pytest.raises(ProjectNotRunning):
        runtime.write_git_credential(project, GitToken("github.com", GITHUB_TOKEN))


def running(runtime: Runtime, user: str) -> str:
    """A new Project of the User, its Remote Control Server ready."""
    project = cloned(runtime, user)
    runtime.start_project(user, project, name="Hello", permission_mode="default")
    server_ready(runtime, project)
    return project


def typed(runtime: Runtime, project: str, command: str, *, until: str) -> str:
    """What a new Terminal of the Project shows once `command` typed in it printed `until`."""
    terminal = Screen(runtime.open_terminal(project))
    try:
        terminal.type(f"{command}\r")
        terminal.wait_for(until)
        return terminal.text
    finally:
        terminal.tty.close()
