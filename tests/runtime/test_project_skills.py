"""Runtime contract: a Project's Skills, read-only under the user-level skills folder of its
environment's Claude config (ADR 0005).

The stand-in for Claude Code (tests/runtime/stand-in) reports what it finds there when it starts.
"""

from collections.abc import Callable

import pytest

from dockyard.runtime import Runtime, Skill, SkillFile

from .conftest import SkillsKept
from .test_project_environment import cloned, reported, server_ready

HELLO = Skill(
    "hello",
    (
        SkillFile("SKILL.md", b"Say hello."),
        SkillFile("scripts/greet.sh", b"echo hello", executable=True),
    ),
)
BYE = Skill("bye", (SkillFile("SKILL.md", b"Say goodbye."),))


def test_a_project_reads_its_skills_under_the_user_skills_folder_executable_bits_kept(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    project = cloned(runtime, user)

    runtime.start_project(user, project, name="Hello", permission_mode="default", skills=[HELLO])

    assert reported(server_ready(runtime, project), "skills") == (
        "hello/SKILL.md 644 Say hello.; hello/scripts/greet.sh 755 echo hello"
    )


def test_a_project_cannot_write_its_skills_but_keeps_writing_the_claude_config(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    project = cloned(runtime, user)

    runtime.start_project(user, project, name="Hello", permission_mode="default", skills=[HELLO])

    log = server_ready(runtime, project)
    assert reported(log, "skills writable") == "no"
    assert reported(log, "claude config writable") == "yes"


def test_a_project_without_skills_finds_the_skills_folder_empty(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    project = cloned(runtime, user)

    runtime.start_project(user, project, name="Hello", permission_mode="default")

    assert reported(server_ready(runtime, project), "skills") == ""


def test_starting_again_with_other_skills_shows_exactly_the_new_ones(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    project = cloned(runtime, user)
    runtime.start_project(user, project, name="Hello", permission_mode="default", skills=[HELLO])
    server_ready(runtime, project)

    runtime.start_project(user, project, name="Hello", permission_mode="default", skills=[BYE])

    log = server_ready(runtime, project)
    assert reported(log, "starts") == "2"
    assert reported(log, "skills") == "bye/SKILL.md 644 Say goodbye."


def test_another_project_of_the_same_user_never_sees_the_skills(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    first, second = cloned(runtime, user), cloned(runtime, user)
    runtime.start_project(user, first, name="First", permission_mode="default", skills=[HELLO])
    server_ready(runtime, first)

    runtime.start_project(user, second, name="Second", permission_mode="default", skills=[BYE])

    assert reported(server_ready(runtime, second), "skills") == "bye/SKILL.md 644 Say goodbye."
    assert reported(server_ready(runtime, first), "skills") == (
        "hello/SKILL.md 644 Say hello.; hello/scripts/greet.sh 755 echo hello"
    )


@pytest.mark.parametrize("code", [True, False])
def test_removing_a_project_removes_its_skills(
    runtime: Runtime, new_user: Callable[[], str], skills_kept: SkillsKept, code: bool
) -> None:
    user = new_user()
    project = cloned(runtime, user)
    runtime.start_project(user, project, name="Hello", permission_mode="default", skills=[HELLO])
    server_ready(runtime, project)
    runtime.stop_project(project)
    assert skills_kept(runtime, project)

    runtime.remove_project(project, code=code)

    assert not skills_kept(runtime, project)
