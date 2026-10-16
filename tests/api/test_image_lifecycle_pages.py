"""The pages of the life of Images: a User changes the Image of a stopped Project from its page and
sees when a newer build is there; an Administrator sees failed builds highlighted and rebuilds."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, AccessProxy, connect_claude_account, wait_until
from .test_image_pages import ITALIAN, REPOSITORY, built

USER = "a.user@example.com"


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


def status_of(dockyard: TestClient, headers: dict[str, str], project: str) -> str:
    status: str = dockyard.get(f"/api/projects/{project}", headers=headers).json()["status"]
    return status


def running(dockyard: TestClient, headers: dict[str, str], **fields: str) -> str:
    body = {"name": "Hello", "repository": REPOSITORY, "visibility": "public", **fields}
    project: str = dockyard.post("/api/projects", json=body, headers=headers).json()["id"]
    assert wait_until(lambda: status_of(dockyard, headers, project) == "running")
    return project


def change(dockyard: TestClient, headers: dict[str, str], project: str, image: str) -> Response:
    return dockyard.post(
        f"/projects/{project}/image", data={"image": image}, headers=headers, follow_redirects=False
    )


def test_a_stopped_project_offers_the_ready_images_and_changes_to_the_one_picked(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    image = built(dockyard, administrator)
    failed = built(dockyard, administrator, label="Broken", instructions="RUN false")
    project = running(dockyard, user)
    dockyard.post(f"/api/projects/{project}/stop", headers=user)

    page = dockyard.get(f"/projects/{project}", headers=user).text

    assert f'action="/projects/{project}/image"' in page
    assert f'value="{image}"' in page
    assert f'value="{failed}"' not in page
    assert "keeps its code and home folder" in page
    changed = change(dockyard, user, project, image)
    assert changed.status_code == 303
    assert changed.headers["location"] == f"/projects/{project}"
    assert "Image: Rust" in dockyard.get(f"/projects/{project}", headers=user).text


def test_a_running_project_explains_that_it_must_stop_to_change_image(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    image = built(dockyard, administrator)
    project = running(dockyard, user)

    page = dockyard.get(f"/projects/{project}", headers=user).text

    assert f'action="/projects/{project}/image"' not in page
    assert "Stop the Project to change its Image" in page
    refused = change(dockyard, user, project, image)
    assert refused.status_code == 409
    assert "Stop this Project first" in refused.text


def test_a_project_on_an_older_build_shows_update_available_on_its_page_and_the_dashboard(
    dockyard: TestClient, user: dict[str, str], runtime: FakeRuntime
) -> None:
    project = running(dockyard, user)
    assert "Update available" not in dockyard.get(f"/projects/{project}", headers=user).text

    runtime.rebuild_base()

    page = dockyard.get(f"/projects/{project}", headers=user).text
    assert "Update available" in page
    assert "Restart it to move to the newer build" in page
    assert "Update available" in dockyard.get("/", headers=user).text


def test_failed_builds_are_highlighted_to_administrators_on_their_dashboard(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    failed = built(dockyard, administrator, label="Broken", instructions="RUN false")

    dashboard = dockyard.get("/", headers=administrator).text

    assert "failed to build" in dashboard
    assert f'href="/images/{failed}"' in dashboard
    assert "failed to build" not in dockyard.get("/", headers=user).text


def test_a_failed_rebuild_says_that_projects_keep_the_previous_build(
    dockyard: TestClient, administrator: dict[str, str], runtime: FakeRuntime
) -> None:
    image = built(dockyard, administrator)

    with runtime.builds_failing():
        rebuilt = dockyard.post(
            f"/images/{image}/rebuild", headers=administrator, follow_redirects=False
        )
        assert rebuilt.status_code == 303
        assert wait_until(
            lambda: (
                dockyard.get(f"/api/images/{image}", headers=administrator).json()["status"]
                == "failed"
            )
        )

    page = dockyard.get(f"/images/{image}", headers=administrator).text
    assert "keep starting from its previous build" in page
    assert f'action="/images/{image}/rebuild"' in page


def test_the_base_image_page_offers_no_rebuild(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    page = dockyard.get("/images/base", headers=administrator).text

    assert "/rebuild" not in page
    assert dockyard.post("/images/base/rebuild", headers=administrator).status_code == 409


def test_only_administrators_rebuild_from_the_image_page(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    image = built(dockyard, administrator)

    assert dockyard.post(f"/images/{image}/rebuild", headers=user).status_code == 403


def test_the_image_lifecycle_speaks_italian_to_an_italian_browser(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str], runtime: FakeRuntime
) -> None:
    built(dockyard, administrator, label="Broken", instructions="RUN false")
    project = running(dockyard, user)
    runtime.rebuild_base()

    assert "Aggiornamento disponibile" in (
        dockyard.get(f"/projects/{project}", headers={**user, **ITALIAN}).text
    )
    assert (
        "Non è stato possibile costruire"
        in dockyard.get("/", headers={**administrator, **ITALIAN}).text
    )
