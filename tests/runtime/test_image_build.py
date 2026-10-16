"""Runtime contract: an Image an Administrator defines, built on top of the Base Image from their
extra instructions, then checked before Users may pick it."""

from collections.abc import Callable

import pytest

from dockyard.runtime import ImageMissing, Runtime

from .conftest import Screen
from .test_project_environment import cloned, server_ready

# Writing below /etc needs root: it works only because the instructions run as root.
WRITE_AS_ROOT = "RUN echo built-by-dockyard > /etc/dockyard-contract"


def build(runtime: Runtime, image: str, instructions: str) -> tuple[bool, str]:
    """Whether the build succeeded, and its log."""
    printed: list[str] = []
    succeeded = runtime.build_image(image, instructions, output=printed.append)
    return succeeded, "".join(printed)


def test_an_image_runs_its_instructions_as_root_and_keeps_the_base_images_non_root_user(
    runtime: Runtime, new_image: Callable[[], str]
) -> None:
    image = new_image()

    succeeded, log = build(runtime, image, WRITE_AS_ROOT)

    assert succeeded, log
    assert "echo built-by-dockyard > /etc/dockyard-contract" in log
    check = runtime.check_image(image)
    assert check.uid == 1000
    assert check.claude  # the Base Image's Claude Code is there


def test_a_project_started_in_an_image_has_what_its_instructions_installed(
    runtime: Runtime, new_image: Callable[[], str], new_user: Callable[[], str]
) -> None:
    image = new_image()
    assert build(runtime, image, WRITE_AS_ROOT)[0]
    user = new_user()
    project = cloned(runtime, user)

    runtime.start_project(user, project, name="Hello", permission_mode="default", image=image)

    server_ready(runtime, project)
    terminal = Screen(runtime.open_terminal(project))
    terminal.type("cat /etc/dockyard-contract\r")
    terminal.wait_for("built-by-dockyard\r\n")


def test_a_project_started_without_an_image_runs_the_base_image(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    project = cloned(runtime, user)

    runtime.start_project(user, project, name="Hello", permission_mode="default")

    server_ready(runtime, project)
    terminal = Screen(runtime.open_terminal(project))
    terminal.type("cat /etc/dockyard-contract\r")
    terminal.wait_for("No such file or directory")


def test_a_failing_instruction_fails_the_build_and_its_log_says_which(
    runtime: Runtime, new_image: Callable[[], str]
) -> None:
    image = new_image()

    succeeded, log = build(runtime, image, "RUN true\nRUN false")

    assert not succeeded
    assert "returned a non-zero code: 1" in log
    with pytest.raises(ImageMissing):
        runtime.check_image(image)


def test_the_check_tells_when_claude_code_is_missing(
    runtime: Runtime, new_image: Callable[[], str]
) -> None:
    image = new_image()
    assert build(runtime, image, "RUN rm /usr/local/bin/claude")[0]

    check = runtime.check_image(image)

    assert check.claude is None
    assert check.uid == 1000


def test_the_check_tells_when_the_default_user_is_root(
    runtime: Runtime, new_image: Callable[[], str]
) -> None:
    image = new_image()
    assert build(runtime, image, "RUN usermod --non-unique --uid 0 ubuntu")[0]

    assert runtime.check_image(image).uid == 0


def test_a_removed_image_is_gone_and_removing_it_again_is_harmless(
    runtime: Runtime, new_image: Callable[[], str]
) -> None:
    image = new_image()
    assert build(runtime, image, WRITE_AS_ROOT)[0]

    runtime.remove_image(image)

    with pytest.raises(ImageMissing):
        runtime.check_image(image)
    runtime.remove_image(image)
