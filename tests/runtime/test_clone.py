"""Runtime contract: cloning a public repository into a new Project, and removing a Project."""

from collections.abc import Callable

from dockyard.runtime import Clone, CloneState, ProjectStatus, Runtime

from .conftest import MISSING_REPOSITORY, PUBLIC_REPOSITORY, new_project, wait_until


def test_a_public_repository_is_cloned_into_the_projects_own_path(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    """git runs as the non-root user: it can write there only if the new volume is its own."""
    project = new_project()

    runtime.start_clone(new_user(), project, PUBLIC_REPOSITORY)

    clone = finished(runtime, project)
    assert clone.state == CloneState.SUCCEEDED, clone.output
    assert f"Cloning into '/workspace/{project}'" in clone.output


def test_two_projects_of_the_same_repository_each_get_their_own_code(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    first, second = new_project(), new_project()

    runtime.start_clone(user, first, PUBLIC_REPOSITORY)
    runtime.start_clone(user, second, PUBLIC_REPOSITORY)

    for project in (first, second):
        clone = finished(runtime, project)
        assert clone.state == CloneState.SUCCEEDED, clone.output
        assert f"Cloning into '/workspace/{project}'" in clone.output


def test_a_project_is_cloning_until_its_clone_is_finished_with_then_its_code_stays(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    project = new_project()
    runtime.start_clone(new_user(), project, PUBLIC_REPOSITORY)
    assert runtime.project_status(project) == ProjectStatus.CLONING
    finished(runtime, project)
    assert runtime.project_status(project) == ProjectStatus.CLONING

    runtime.finish_clone(project)

    assert runtime.clone(project) is None
    assert runtime.project_status(project) == ProjectStatus.STOPPED


def test_a_repository_that_cannot_be_cloned_fails_with_gits_error(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    project = new_project()

    runtime.start_clone(new_user(), project, MISSING_REPOSITORY)

    clone = finished(runtime, project)
    assert clone.state == CloneState.FAILED
    assert "fatal:" in clone.output


def test_removing_a_project_with_its_code_leaves_nothing(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    project = new_project()
    runtime.start_clone(new_user(), project, MISSING_REPOSITORY)
    finished(runtime, project)

    runtime.remove_project(project, code=True)

    assert runtime.clone(project) is None
    assert runtime.project_status(project) == ProjectStatus.MISSING


def test_removing_a_project_without_its_code_keeps_the_code(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    project = new_project()
    runtime.start_clone(new_user(), project, PUBLIC_REPOSITORY)
    finished(runtime, project)
    runtime.finish_clone(project)

    runtime.remove_project(project, code=False)

    assert runtime.project_status(project) == ProjectStatus.STOPPED
    runtime.remove_project(project, code=True)
    assert runtime.project_status(project) == ProjectStatus.MISSING


def test_removing_a_project_while_it_clones_stops_the_clone(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    project = new_project()
    runtime.start_clone(new_user(), project, PUBLIC_REPOSITORY)

    runtime.remove_project(project, code=True)

    assert runtime.clone(project) is None
    assert runtime.project_status(project) == ProjectStatus.MISSING
    runtime.remove_project(project, code=True)  # removing again is harmless


def finished(runtime: Runtime, project: str) -> Clone:
    """The Project's clone, once it has ended."""
    assert wait_until(lambda: _state(runtime, project) != CloneState.RUNNING, timeout=120)
    clone = runtime.clone(project)
    assert clone is not None
    return clone


def _state(runtime: Runtime, project: str) -> CloneState | None:
    clone = runtime.clone(project)
    return clone.state if clone else None
