"""Runtime contract: Images built again. A Project keeps the build it started from until it starts
again, and can tell when a newer build is there; a build a Project runs is never removed."""

from collections.abc import Callable

import pytest

from dockyard.runtime import ImageMissing, Runtime

from .conftest import RebuildBase, Screen
from .test_image_build import WRITE_AS_ROOT, build
from .test_project_environment import cloned, reported, server_ready

WRITE_AS_ROOT_AGAIN = "RUN echo built-again > /etc/dockyard-contract"


def test_a_project_moved_to_another_image_keeps_its_code_and_home(
    runtime: Runtime, new_image: Callable[[], str], new_user: Callable[[], str]
) -> None:
    image = new_image()
    assert build(runtime, image, WRITE_AS_ROOT)[0]
    user = new_user()
    project = cloned(runtime, user)
    runtime.start_project(user, project, name="Hello", permission_mode="default")
    server_ready(runtime, project)
    runtime.stop_project(project)

    runtime.start_project(user, project, name="Hello", permission_mode="default", image=image)

    log = server_ready(runtime, project)
    assert reported(log, "starts") == "2"  # counted in the home
    assert reported(log, "code") == "Merge pull request #6 from Spaceghost/patch-1"
    terminal = Screen(runtime.open_terminal(project))
    terminal.type("cat /etc/dockyard-contract\r")
    terminal.wait_for("built-by-dockyard\r\n")


def test_a_project_on_an_older_build_is_outdated_until_it_starts_on_the_newer_one(
    runtime: Runtime, new_image: Callable[[], str], new_user: Callable[[], str]
) -> None:
    older, newer = new_image(), new_image()
    assert build(runtime, older, WRITE_AS_ROOT)[0]
    user = new_user()
    project = cloned(runtime, user)
    runtime.start_project(user, project, name="Hello", permission_mode="default", image=older)
    server_ready(runtime, project)
    assert not runtime.project_outdated(project, older)

    assert build(runtime, newer, WRITE_AS_ROOT_AGAIN)[0]

    assert runtime.project_outdated(project, newer)
    terminal = Screen(runtime.open_terminal(project))
    terminal.type("cat /etc/dockyard-contract\r")
    terminal.wait_for("built-by-dockyard\r\n")  # it keeps running the older build
    runtime.start_project(user, project, name="Hello", permission_mode="default", image=newer)
    server_ready(runtime, project)
    assert not runtime.project_outdated(project, newer)
    terminal = Screen(runtime.open_terminal(project))
    terminal.type("cat /etc/dockyard-contract\r")
    terminal.wait_for("built-again\r\n")


def test_a_project_on_the_base_image_is_outdated_once_the_base_image_is_built_again(
    own_base: tuple[Runtime, RebuildBase], new_user: Callable[[], str]
) -> None:
    runtime, rebuild_base = own_base
    user = new_user()
    project = cloned(runtime, user)
    runtime.start_project(user, project, name="Hello", permission_mode="default")
    server_ready(runtime, project)
    before = runtime.base_build()
    assert runtime.base_build() == before
    assert not runtime.project_outdated(project, None)

    rebuild_base()

    assert runtime.base_build() != before
    assert runtime.project_outdated(project, None)
    runtime.start_project(user, project, name="Hello", permission_mode="default")
    server_ready(runtime, project)
    assert not runtime.project_outdated(project, None)


def test_a_stopped_project_is_never_outdated(
    own_base: tuple[Runtime, RebuildBase], new_user: Callable[[], str]
) -> None:
    runtime, rebuild_base = own_base
    user = new_user()
    project = cloned(runtime, user)
    runtime.start_project(user, project, name="Hello", permission_mode="default")
    server_ready(runtime, project)
    runtime.stop_project(project)

    rebuild_base()

    assert not runtime.project_outdated(project, None)  # it starts on the newer build


def test_a_build_a_project_runs_is_kept_until_the_project_stops(
    runtime: Runtime, new_image: Callable[[], str], new_user: Callable[[], str]
) -> None:
    image = new_image()
    assert build(runtime, image, WRITE_AS_ROOT)[0]
    user = new_user()
    project = cloned(runtime, user)
    runtime.start_project(user, project, name="Hello", permission_mode="default", image=image)
    server_ready(runtime, project)

    assert not runtime.remove_image(image)

    assert runtime.check_image(image).claude  # still there
    runtime.stop_project(project)
    assert runtime.remove_image(image)
    with pytest.raises(ImageMissing):
        runtime.check_image(image)
    assert runtime.remove_image(image)  # removing it again is harmless
