"""The pages where an Administrator creates an Image, follows its build and deletes it, and where a
User picks a ready Image for a new Project."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, AccessProxy, wait_until

USER = "a.user@example.com"
REPOSITORY = "https://github.com/octocat/Hello-World"
ITALIAN = {"Accept-Language": "it-IT,it;q=0.9"}
INSTRUCTIONS = "RUN echo rust-toolchain > /etc/rust"


@pytest.fixture(autouse=True)
def public_repository(runtime: FakeRuntime) -> None:
    runtime.publish_repository(REPOSITORY)


@pytest.fixture
def administrator(proxy: AccessProxy) -> dict[str, str]:
    return proxy.headers(ADMINISTRATOR)


@pytest.fixture
def user(proxy: AccessProxy, add_user: Callable[[str], None]) -> dict[str, str]:
    add_user(USER)
    return proxy.headers(USER)


def submit(dockyard: TestClient, headers: dict[str, str], **fields: str) -> Response:
    form = {
        "label": "Rust",
        "description": "The Rust toolchain.",
        "instructions": INSTRUCTIONS,
        **fields,
    }
    return dockyard.post("/images", data=form, headers=headers, follow_redirects=False)


def built(dockyard: TestClient, headers: dict[str, str], **fields: str) -> str:
    """A new Image, by its id, once its build and check have ended."""
    image: str = submit(dockyard, headers, **fields).headers["location"].removeprefix("/images/")
    assert wait_until(
        lambda: dockyard.get(f"/api/images/{image}", headers=headers).json()["status"] != "building"
    )
    return image


def test_the_dashboard_leads_administrators_to_the_images(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    assert 'href="/images"' in dockyard.get("/", headers=administrator).text
    assert 'href="/images"' not in dockyard.get("/", headers=user).text


def test_only_administrators_reach_the_image_pages(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    image = built(dockyard, administrator)

    for path in ("/images", "/images/new", f"/images/{image}"):
        assert dockyard.get(path, headers=user).status_code == 403
    assert submit(dockyard, user).status_code == 403
    assert dockyard.post(f"/images/{image}/delete", headers=user).status_code == 403


def test_an_administrator_creates_an_image_and_follows_its_build_on_its_page(
    dockyard: TestClient, administrator: dict[str, str], runtime: FakeRuntime
) -> None:
    form = dockyard.get("/images/new", headers=administrator)
    assert 'name="instructions"' in form.text
    assert "runs them as root" in form.text

    with runtime.builds_paused():
        created = submit(dockyard, administrator)
        assert created.status_code == 303
        building = dockyard.get(created.headers["location"], headers=administrator)
        assert "Building" in building.text
        assert "Dockyard is building this Image" in building.text
    image = created.headers["location"].removeprefix("/images/")
    assert wait_until(
        lambda: "Ready" in dockyard.get(f"/images/{image}", headers=administrator).text
    )

    page = dockyard.get(f"/images/{image}", headers=administrator)
    assert "Rust" in page.text
    assert "The Rust toolchain." in page.text
    assert "echo rust-toolchain &gt; /etc/rust" in page.text  # its instructions and its log
    assert "Users can pick this Image" in page.text


def test_a_refused_image_form_says_why_and_keeps_what_was_typed(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    refused = submit(dockyard, administrator, instructions="FROM ubuntu:24.04\nRUN true")

    assert refused.status_code == 422
    assert "Remove the FROM instruction" in refused.text
    assert "FROM ubuntu:24.04" in refused.text
    assert 'value="Rust"' in refused.text


def test_a_failed_image_says_why_it_cannot_be_picked(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    image = built(dockyard, administrator, instructions="RUN rm /usr/local/bin/claude")

    page = dockyard.get(f"/images/{image}", headers=administrator)

    assert "Failed" in page.text
    assert "Claude Code is missing from it" in page.text


def test_the_images_page_lists_every_image_with_its_status(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    ready = built(dockyard, administrator)
    failed = built(dockyard, administrator, label="Broken", instructions="RUN false")

    page = dockyard.get("/images", headers=administrator)

    assert 'href="/images/base"' in page.text
    assert "Base Image" in page.text
    assert f'href="/images/{ready}"' in page.text
    assert f'href="/images/{failed}"' in page.text
    assert "Broken" in page.text
    assert "Failed" in page.text
    assert 'href="/images/new"' in page.text


def test_the_base_image_page_offers_no_deletion(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    page = dockyard.get("/images/base", headers=administrator)

    assert "Base Image" in page.text
    assert "cannot be deleted" in page.text
    assert "/images/base/delete" not in page.text
    assert dockyard.post("/images/base/delete", headers=administrator).status_code == 409


def test_an_unused_image_is_deleted_from_its_page(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    image = built(dockyard, administrator)

    deleted = dockyard.post(
        f"/images/{image}/delete", headers=administrator, follow_redirects=False
    )

    assert deleted.status_code == 303
    assert deleted.headers["location"] == "/images"
    assert dockyard.get(f"/images/{image}", headers=administrator).status_code == 404


def test_an_image_in_use_lists_its_projects_and_cannot_be_deleted_from_its_page(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    image = built(dockyard, administrator)
    form = {"name": "Rusty", "repository": REPOSITORY, "visibility": "public", "image": image}
    dockyard.post("/projects", data=form, headers=user)

    page = dockyard.get(f"/images/{image}", headers=administrator)
    refused = dockyard.post(f"/images/{image}/delete", headers=administrator)

    assert "Rusty" in page.text
    assert USER in page.text
    assert f"/images/{image}/delete" not in page.text
    assert refused.status_code == 409
    assert "the Projects listed below use it" in refused.text
    assert "Rusty" in refused.text


def test_the_new_project_form_offers_ready_images_with_their_description(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    ready = built(dockyard, administrator)
    failed = built(dockyard, administrator, label="Broken", instructions="RUN false")

    form = dockyard.get("/projects/new", headers=user)

    assert 'name="image" value="base" checked' in form.text
    assert "Claude Code, git, gh, glab, Node and Python." in form.text
    assert f'name="image" value="{ready}"' in form.text
    assert "The Rust toolchain." in form.text
    assert failed not in form.text
    assert "Broken" not in form.text


def test_a_project_created_with_an_image_shows_it(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    image = built(dockyard, administrator)
    form = {"name": "Rusty", "repository": REPOSITORY, "visibility": "public", "image": image}

    created = dockyard.post("/projects", data=form, headers=user, follow_redirects=False)

    assert created.status_code == 303
    page = dockyard.get(created.headers["location"], headers=user)
    assert "Image: Rust" in page.text


def test_a_project_refused_an_image_keeps_the_form_and_says_why(
    dockyard: TestClient, user: dict[str, str]
) -> None:
    form = {"name": "Rusty", "repository": REPOSITORY, "visibility": "public", "image": "gone"}

    refused = dockyard.post("/projects", data=form, headers=user)

    assert refused.status_code == 422
    assert "This Image cannot be picked" in refused.text
    assert 'value="Rusty"' in refused.text


def test_the_image_pages_speak_italian_to_an_italian_browser(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    image = built(dockyard, administrator)

    images = dockyard.get("/images", headers={**administrator, **ITALIAN})
    page = dockyard.get(f"/images/{image}", headers={**administrator, **ITALIAN})
    form = dockyard.get("/projects/new", headers={**user, **ITALIAN})

    assert "Immagine base" in images.text
    assert "Pronta" in images.text
    assert "Log della build" in page.text
    assert "Immagine base" in form.text
    assert "Claude Code, git, gh, glab, Node e Python." in form.text


def test_a_description_on_several_lines_is_kept_as_typed(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    image = built(dockyard, administrator, description="Rust.\r\nAnd cargo.")

    shown = dockyard.get(f"/api/images/{image}", headers=administrator).json()

    assert shown["description"] == "Rust.\nAnd cargo."
