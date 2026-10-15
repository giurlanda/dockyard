"""The pages where a User creates, follows and deletes their Projects."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from dockyard.runtime import ProjectStatus
from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, AccessProxy, wait_until

REPOSITORY = "https://github.com/octocat/Hello-World"
MISSING_REPOSITORY = "https://github.com/octocat/no-such-repository"


@pytest.fixture(autouse=True)
def public_repository(runtime: FakeRuntime) -> None:
    runtime.publish_repository(REPOSITORY)


def submit(
    dockyard: TestClient,
    headers: dict[str, str],
    *,
    name: str = "Hello",
    repository: str = REPOSITORY,
) -> Response:
    return dockyard.post(
        "/projects",
        data={"name": name, "repository": repository, "visibility": "public"},
        headers=headers,
    )


def test_the_dashboard_leads_to_the_new_project_form(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    assert 'href="/projects/new"' in dockyard.get("/", headers=signed_in).text

    form = dockyard.get("/projects/new", headers=signed_in)

    assert form.status_code == 200
    assert "Repository address" in form.text
    assert "Remote Control Server" in form.text
    assert "Public" in form.text


def test_creating_a_project_opens_its_page_and_the_dashboard_lists_it_with_its_status(
    dockyard: TestClient, proxy: AccessProxy, runtime: FakeRuntime
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)

    with runtime.clones_paused():
        page = submit(dockyard, signed_in, name="Hello World")
        assert page.status_code == 200
        assert page.url.path.startswith("/projects/")
        assert "Cloning" in page.text
        assert "Clone progress" in page.text
        dashboard = dockyard.get("/", headers=signed_in).text
        assert "Hello World" in dashboard
        assert "Cloning" in dashboard
        assert "You have no Projects yet." not in dashboard

    assert wait_until(lambda: "Stopped" in dockyard.get("/", headers=signed_in).text)


def test_a_refused_address_is_explained_on_the_form_which_keeps_what_was_typed(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)

    page = submit(dockyard, signed_in, name="Mine", repository="https://bitbucket.org/me/mine")

    assert page.status_code == 422
    assert "Only repositories on github.com and gitlab.com are supported." in page.text
    assert 'value="https://bitbucket.org/me/mine"' in page.text
    assert 'value="Mine"' in page.text


def test_a_failed_clone_shows_gits_error_and_offers_to_try_again(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    created = submit(dockyard, signed_in, name="Oops", repository=MISSING_REPOSITORY)
    path = created.url.path

    assert wait_until(lambda: "The clone failed" in dockyard.get(path, headers=signed_in).text)
    page = dockyard.get(path, headers=signed_in).text
    assert "fatal: could not read Username" in page
    assert "the Project has been removed" in page
    assert "/projects/new?name=Oops&amp;repository=https" in page
    assert "Oops" not in dockyard.get("/", headers=signed_in).text


def test_the_new_project_form_can_be_filled_in_from_the_address(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    form = dockyard.get(
        "/projects/new",
        params={"name": "Again", "repository": REPOSITORY},
        headers=proxy.headers(ADMINISTRATOR),
    )

    assert 'value="Again"' in form.text
    assert f'value="{REPOSITORY}"' in form.text


@pytest.mark.parametrize(
    ("form", "code"), [({}, ProjectStatus.STOPPED), ({"delete_code": "on"}, ProjectStatus.MISSING)]
)
def test_deleting_from_the_project_page_removes_the_code_only_when_ticked(
    dockyard: TestClient,
    proxy: AccessProxy,
    runtime: FakeRuntime,
    form: dict[str, str],
    code: ProjectStatus,
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    path = submit(dockyard, signed_in).url.path
    page = dockyard.get(path, headers=signed_in).text
    assert "Also delete its code" in page

    dashboard = dockyard.post(f"{path}/delete", data=form, headers=signed_in)

    assert dashboard.url.path == "/"
    assert "You have no Projects yet." in dashboard.text
    assert runtime.project_status(path.rsplit("/", 1)[-1]) == code


def test_another_users_project_page_does_not_exist_for_them(
    dockyard: TestClient, proxy: AccessProxy, add_user: Callable[[str], None]
) -> None:
    add_user("another.user@example.com")
    path = submit(dockyard, proxy.headers(ADMINISTRATOR)).url.path
    other = proxy.headers("another.user@example.com")

    assert dockyard.get(path, headers=other).status_code == 404
    assert dockyard.post(f"{path}/delete", headers=other).status_code == 404
    assert dockyard.get(path, headers=proxy.headers(ADMINISTRATOR)).status_code == 200


def test_the_project_pages_speak_italian(dockyard: TestClient, proxy: AccessProxy) -> None:
    signed_in = {**proxy.headers(ADMINISTRATOR), "Accept-Language": "it"}

    form = dockyard.get("/projects/new", headers=signed_in)
    refused = submit(dockyard, signed_in, repository="https://bitbucket.org/me/mine")
    page = submit(dockyard, signed_in)

    assert "Indirizzo del repository" in form.text
    assert "Sono supportati solo repository su github.com e gitlab.com." in refused.text
    assert "Elimina il Progetto" in page.text
