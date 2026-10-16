"""The life of Images after their first build: a stopped Project changes Image and keeps its code
and home; a Project on an older build of its Image shows "update available" and moves to the new
build when it starts again; a new Base Image rebuilds every Image."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, AccessProxy, connect_claude_account, wait_until
from .test_images import REPOSITORY, built, create_project, shown, terminal_shows

USER = "a.user@example.com"
OTHER_USER = "another.user@example.com"


@pytest.fixture(autouse=True)
def public_repository(runtime: FakeRuntime) -> None:
    runtime.publish_repository(REPOSITORY)


@pytest.fixture
def administrator(proxy: AccessProxy) -> dict[str, str]:
    return proxy.headers(ADMINISTRATOR)


@pytest.fixture
def user(
    dockyard: TestClient, proxy: AccessProxy, add_user: Callable[[str], None]
) -> dict[str, str]:
    add_user(USER)
    headers = proxy.headers(USER)
    connect_claude_account(dockyard, headers)
    return proxy.headers(USER)  # the sign-in's socket changed those


def project_of(dockyard: TestClient, headers: dict[str, str], project: str) -> Any:
    return dockyard.get(f"/api/projects/{project}", headers=headers).json()


def running(dockyard: TestClient, headers: dict[str, str], **fields: str) -> str:
    """A new Project of the User, once its Remote Control Server runs."""
    project = create_project(dockyard, headers, **fields).json()["id"]
    assert wait_until(lambda: project_of(dockyard, headers, project)["status"] == "running")
    return str(project)


def stopped(dockyard: TestClient, headers: dict[str, str], **fields: str) -> str:
    project = running(dockyard, headers, **fields)
    dockyard.post(f"/api/projects/{project}/stop", headers=headers)
    return project


def change_image(dockyard: TestClient, headers: dict[str, str], project: str, image: str) -> Any:
    return dockyard.patch(f"/api/projects/{project}", json={"image": image}, headers=headers)


def test_a_stopped_project_changes_image_and_keeps_its_code_and_home(
    dockyard: TestClient,
    administrator: dict[str, str],
    user: dict[str, str],
    runtime: FakeRuntime,
) -> None:
    image = built(dockyard, administrator)["id"]
    project = stopped(dockyard, user)

    changed = change_image(dockyard, user, project, image)

    assert changed.status_code == 200, changed.text
    assert changed.json()["image"] == image
    assert changed.json()["status"] == "stopped"
    dockyard.post(f"/api/projects/{project}/start", headers=user)
    assert "rust-toolchain" in terminal_shows(dockyard, user, project, "cat /etc/rust")
    log = runtime.project_log(project)
    assert "starts: 2" in log  # counted in its home, which it kept
    assert "code: Merge pull request #6" in log  # its code too


def test_a_project_changes_back_to_the_base_image(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    image = built(dockyard, administrator)["id"]
    project = stopped(dockyard, user, image=image)

    assert change_image(dockyard, user, project, "base").json()["image"] == "base"

    dockyard.post(f"/api/projects/{project}/start", headers=user)
    assert "No such file" in terminal_shows(dockyard, user, project, "cat /etc/rust")


@pytest.mark.parametrize("state", ["running", "waiting", "error"])
def test_the_image_of_a_project_that_is_not_stopped_cannot_change(
    dockyard: TestClient,
    administrator: dict[str, str],
    user: dict[str, str],
    runtime: FakeRuntime,
    state: str,
) -> None:
    image = built(dockyard, administrator)["id"]
    owner = user
    if state == "waiting":  # the Administrator has not connected a Claude Account
        owner = administrator
        project = create_project(dockyard, owner).json()["id"]
        assert wait_until(lambda: project_of(dockyard, owner, project)["status"] == "waiting")
    else:
        project = running(dockyard, owner)
    if state == "error":
        runtime.crash(project)
    assert project_of(dockyard, owner, project)["status"] == state

    refused = change_image(dockyard, owner, project, image)

    assert refused.status_code == 409
    assert refused.json()["error"] == "not_stopped"
    assert refused.json()["message"]
    assert project_of(dockyard, owner, project)["image"] == "base"


def test_a_project_cannot_change_to_an_image_that_is_not_ready(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    failed = built(dockyard, administrator, instructions="RUN false")["id"]
    project = stopped(dockyard, user)

    for image in (failed, "no-such-image"):
        refused = change_image(dockyard, user, project, image)

        assert refused.status_code == 422
        assert refused.json()["error"] == "image_not_available"
    assert project_of(dockyard, user, project)["image"] == "base"


def test_a_user_cannot_change_the_image_of_another_users_project(
    dockyard: TestClient,
    administrator: dict[str, str],
    user: dict[str, str],
    proxy: AccessProxy,
    add_user: Callable[[str], None],
) -> None:
    image = built(dockyard, administrator)["id"]
    project = stopped(dockyard, user)
    add_user(OTHER_USER)

    assert change_image(dockyard, proxy.headers(OTHER_USER), project, image).status_code == 404
    assert change_image(dockyard, administrator, project, image).status_code == 404
    assert project_of(dockyard, user, project)["image"] == "base"


def rebuild(dockyard: TestClient, headers: dict[str, str], image: str) -> Any:
    return dockyard.post(f"/api/images/{image}/rebuild", headers=headers)


def offered(dockyard: TestClient, headers: dict[str, str]) -> list[str]:
    return [image["id"] for image in dockyard.get("/api/images", headers=headers).json()]


def restart(dockyard: TestClient, headers: dict[str, str], project: str) -> None:
    dockyard.post(f"/api/projects/{project}/restart", headers=headers)
    assert wait_until(lambda: project_of(dockyard, headers, project)["status"] == "running")


def test_a_project_on_an_older_base_image_shows_update_available_until_it_restarts(
    dockyard: TestClient, user: dict[str, str], runtime: FakeRuntime
) -> None:
    project = running(dockyard, user)
    assert project_of(dockyard, user, project)["update_available"] is False

    runtime.rebuild_base()

    assert project_of(dockyard, user, project)["update_available"] is True
    assert project_of(dockyard, user, project)["status"] == "running"
    restart(dockyard, user, project)
    assert project_of(dockyard, user, project)["update_available"] is False


def test_a_stopped_project_never_shows_update_available(
    dockyard: TestClient, user: dict[str, str], runtime: FakeRuntime
) -> None:
    project = stopped(dockyard, user)

    runtime.rebuild_base()

    assert project_of(dockyard, user, project)["update_available"] is False  # it starts on it


def test_a_new_base_image_rebuilds_every_image_when_dockyard_starts(
    dockyard: TestClient,
    start_dockyard: Callable[..., TestClient],
    administrator: dict[str, str],
    runtime: FakeRuntime,
) -> None:
    ready = built(dockyard, administrator)["id"]
    failed = built(dockyard, administrator, label="Broken", instructions="RUN false")["id"]
    runtime.rebuild_base()

    with runtime.builds_paused():
        restarted = start_dockyard()

        assert shown(restarted, administrator, ready)["status"] == "building"
        assert shown(restarted, administrator, failed)["status"] == "building"

    assert wait_until(lambda: shown(restarted, administrator, ready)["status"] == "ready")
    assert wait_until(lambda: shown(restarted, administrator, failed)["status"] == "failed")


def test_dockyard_started_again_on_the_same_base_image_rebuilds_nothing(
    dockyard: TestClient,
    start_dockyard: Callable[..., TestClient],
    administrator: dict[str, str],
) -> None:
    image = built(dockyard, administrator)

    restarted = start_dockyard()

    assert shown(restarted, administrator, image["id"]) == image  # its log too: not built again


def test_a_project_moves_to_the_new_build_of_its_image_when_it_restarts(
    dockyard: TestClient,
    start_dockyard: Callable[..., TestClient],
    administrator: dict[str, str],
    user: dict[str, str],
    runtime: FakeRuntime,
) -> None:
    image = built(dockyard, administrator)["id"]
    project = running(dockyard, user, image=image)
    runtime.rebuild_base()

    restarted = start_dockyard()

    assert wait_until(lambda: shown(restarted, administrator, image)["status"] == "ready")
    assert project_of(restarted, user, project)["update_available"] is True
    restart(restarted, user, project)
    assert project_of(restarted, user, project)["update_available"] is False
    assert "rust-toolchain" in terminal_shows(restarted, user, project, "cat /etc/rust")


def test_a_failed_rebuild_is_highlighted_and_its_projects_keep_the_previous_build(
    dockyard: TestClient,
    start_dockyard: Callable[..., TestClient],
    administrator: dict[str, str],
    user: dict[str, str],
    runtime: FakeRuntime,
) -> None:
    image = built(dockyard, administrator)["id"]
    project = running(dockyard, user, image=image)
    runtime.rebuild_base()

    with runtime.builds_failing():
        restarted = start_dockyard()
        assert wait_until(lambda: shown(restarted, administrator, image)["status"] == "failed")

    assert shown(restarted, administrator, image)["failure"] == "build"
    assert image in offered(restarted, user)  # its previous build passed its check
    assert project_of(restarted, user, project)["update_available"] is False
    restart(restarted, user, project)
    assert "rust-toolchain" in terminal_shows(restarted, user, project, "cat /etc/rust")


def test_an_administrator_rebuilds_an_image_whose_build_failed(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str], runtime: FakeRuntime
) -> None:
    with runtime.builds_failing():
        image = built(dockyard, administrator)["id"]
    assert image not in offered(dockyard, user)

    with runtime.builds_paused():
        rebuilt = rebuild(dockyard, administrator, image)

        assert rebuilt.status_code == 202
        assert rebuilt.json()["status"] == "building"
        assert rebuild(dockyard, administrator, image).json()["error"] == "building"

    assert wait_until(lambda: shown(dockyard, administrator, image)["status"] == "ready")
    assert image in offered(dockyard, user)


def test_only_administrators_rebuild_images_and_never_the_base_image(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    image = built(dockyard, administrator)["id"]

    assert rebuild(dockyard, user, image).status_code == 403
    refused = rebuild(dockyard, administrator, "base")
    assert refused.status_code == 409
    assert refused.json()["error"] == "base_image_not_built"
    assert refused.json()["message"]
    assert rebuild(dockyard, administrator, "no-such-image").status_code == 404


def test_the_previous_build_of_an_image_is_removed_once_no_project_runs_it(
    dockyard: TestClient,
    start_dockyard: Callable[..., TestClient],
    administrator: dict[str, str],
    user: dict[str, str],
    runtime: FakeRuntime,
) -> None:
    image = built(dockyard, administrator)["id"]
    project = running(dockyard, user, image=image)

    rebuild(dockyard, administrator, image)
    assert wait_until(lambda: shown(dockyard, administrator, image)["status"] == "ready")

    assert len(runtime.builds()) == 2  # its Project still runs the previous one
    restart(dockyard, user, project)
    start_dockyard()
    assert len(runtime.builds()) == 1


def test_a_deleted_image_leaves_no_build_behind(
    dockyard: TestClient, administrator: dict[str, str], runtime: FakeRuntime
) -> None:
    image = built(dockyard, administrator)["id"]

    dockyard.delete(f"/api/images/{image}", headers=administrator)

    assert runtime.builds() == set()


def test_an_image_being_built_again_can_still_be_picked_from_its_previous_build(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str], runtime: FakeRuntime
) -> None:
    image = built(dockyard, administrator)["id"]
    project = stopped(dockyard, user)

    with runtime.builds_paused():
        rebuild(dockyard, administrator, image)

        assert image in offered(dockyard, user)
        assert change_image(dockyard, user, project, image).status_code == 200
        assert create_project(dockyard, user, image=image, name="Other").status_code == 202
