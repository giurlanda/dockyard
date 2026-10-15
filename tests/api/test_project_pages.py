"""The pages where a User creates, follows and deletes their Projects."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from dockyard.runtime import ProjectStatus
from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, AccessProxy, connect_claude_account, wait_until

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

    assert wait_until(
        lambda: "Waiting for Claude Account" in dockyard.get("/", headers=signed_in).text
    )


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


def test_the_new_project_form_offers_the_permission_modes_and_warns_about_bypassing_them(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    form = dockyard.get("/projects/new", headers=proxy.headers(ADMINISTRATOR)).text

    for mode in ("default", "acceptEdits", "auto", "bypassPermissions"):
        assert f'name="permission_mode" value="{mode}"' in form
    assert 'value="default" checked' in form
    assert "Only for repositories you fully trust" in form


def test_the_chosen_permission_mode_is_shown_on_the_project_page(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)

    page = dockyard.post(
        "/projects",
        data={
            "name": "Hello",
            "repository": REPOSITORY,
            "visibility": "public",
            "permission_mode": "bypassPermissions",
        },
        headers=signed_in,
    )

    assert "Bypass permissions" in page.text
    assert "Only for repositories you fully trust" in page.text


def test_a_waiting_project_page_explains_it_starts_once_the_claude_account_is_connected(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    path = submit(dockyard, signed_in).url.path

    assert wait_until(
        lambda: "Waiting for Claude Account" in dockyard.get(path, headers=signed_in).text
    )
    page = dockyard.get(path, headers=signed_in).text
    assert "starts by itself as soon as your Claude Account is connected" in page
    assert "The first Session works directly in the main checkout" in page
    assert "its own git worktree" in page
    assert f'action="{path}/start"' in page  # it may start now, if the account is connected


def test_a_user_stops_starts_and_restarts_a_project_from_its_page(
    dockyard: TestClient, proxy: AccessProxy, runtime: FakeRuntime
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    connect_claude_account(dockyard, signed_in)
    path = submit(dockyard, signed_in, name="Hello World").url.path
    assert wait_until(lambda: "Running" in dockyard.get(path, headers=signed_in).text)
    page = dockyard.get(path, headers=signed_in).text
    assert "this Project appears there as “Hello World”" in page

    stopped = dockyard.post(f"{path}/stop", headers=signed_in)
    assert stopped.url.path == path
    assert "The Remote Control Server is stopped" in stopped.text
    started = dockyard.post(f"{path}/start", headers=signed_in)
    assert "this Project appears there as “Hello World”" in started.text
    restarted = dockyard.post(f"{path}/restart", headers=signed_in)
    assert "Running" in restarted.text
    assert "starts: 3" in runtime.project_log(path.rsplit("/", 1)[-1])


def test_another_user_cannot_start_or_stop_a_project_from_its_page(
    dockyard: TestClient, proxy: AccessProxy, add_user: Callable[[str], None]
) -> None:
    add_user("another.user@example.com")
    path = submit(dockyard, proxy.headers(ADMINISTRATOR)).url.path
    other = proxy.headers("another.user@example.com")

    for action in ("start", "stop", "restart"):
        assert dockyard.post(f"{path}/{action}", headers=other).status_code == 404


def test_the_running_project_page_speaks_italian(dockyard: TestClient, proxy: AccessProxy) -> None:
    signed_in = {**proxy.headers(ADMINISTRATOR), "Accept-Language": "it"}
    connect_claude_account(dockyard, signed_in)
    path = submit(dockyard, signed_in).url.path

    assert wait_until(lambda: "In esecuzione" in dockyard.get(path, headers=signed_in).text)
    page = dockyard.get(path, headers=signed_in).text
    assert "La prima Sessione lavora direttamente nella copia principale" in page
    assert "Riavvia" in page
    assert "Ferma" in page


def running_project_page(dockyard: TestClient, headers: dict[str, str]) -> str:
    """The path of a new Project's page, once its Remote Control Server runs."""
    connect_claude_account(dockyard, headers)
    path = submit(dockyard, headers).url.path
    assert wait_until(lambda: "Running" in dockyard.get(path, headers=headers).text)
    return path


def test_a_running_project_page_shows_its_usage_and_leads_to_its_log_and_console(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    path = running_project_page(dockyard, signed_in)
    project = path.rsplit("/", 1)[-1]

    page = dockyard.get(path, headers=signed_in).text

    assert "CPU" in page
    assert "RAM" in page
    assert f'data-stats="/api/projects/{project}/stats"' in page
    assert f'href="{path}/logs"' in page
    assert f'href="{path}/console"' in page


def test_a_stopped_project_page_offers_neither_usage_nor_log_nor_console(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    path = running_project_page(dockyard, signed_in)

    page = dockyard.post(f"{path}/stop", headers=signed_in).text

    assert "data-stats" not in page
    assert f'href="{path}/logs"' not in page
    assert f'href="{path}/console"' not in page


def test_the_log_page_follows_the_log_read_only(dockyard: TestClient, proxy: AccessProxy) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    path = running_project_page(dockyard, signed_in)
    project = path.rsplit("/", 1)[-1]

    page = dockyard.get(f"{path}/logs", headers=signed_in)

    assert page.status_code == 200
    assert f'data-terminal="/ws/projects/{project}/logs"' in page.text
    assert "data-terminal-readonly" in page.text
    assert f'href="{path}"' in page.text


def test_the_console_page_warns_that_ctrl_c_stops_the_server(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    path = running_project_page(dockyard, signed_in)
    project = path.rsplit("/", 1)[-1]

    page = dockyard.get(f"{path}/console", headers=signed_in)

    assert page.status_code == 200
    assert f'data-terminal="/ws/projects/{project}/console"' in page.text
    assert "data-terminal-readonly" not in page.text
    assert "Ctrl+C" in page.text
    assert "stops the Remote Control Server" in page.text


def test_a_project_asking_for_trust_says_to_open_the_console(
    dockyard: TestClient, proxy: AccessProxy, runtime: FakeRuntime
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    path = running_project_page(dockyard, signed_in)

    runtime.lose_trust(path.rsplit("/", 1)[-1])

    page = dockyard.get(path, headers=signed_in).text
    assert "Waiting for your answer" in page
    assert "asks whether to trust this Project" in page
    assert f'href="{path}/console"' in page
    assert "Waiting for your answer" in dockyard.get("/", headers=signed_in).text


def test_the_log_console_and_trust_prompt_speak_italian(
    dockyard: TestClient, proxy: AccessProxy, runtime: FakeRuntime
) -> None:
    signed_in = {**proxy.headers(ADMINISTRATOR), "Accept-Language": "it"}
    connect_claude_account(dockyard, signed_in)
    path = submit(dockyard, signed_in).url.path
    assert wait_until(lambda: "In esecuzione" in dockyard.get(path, headers=signed_in).text)
    runtime.lose_trust(path.rsplit("/", 1)[-1])

    page = dockyard.get(path, headers=signed_in).text
    console = dockyard.get(f"{path}/console", headers=signed_in).text
    log = dockyard.get(f"{path}/logs", headers=signed_in).text

    assert "Attende la tua risposta" in page
    assert "Apri la console" in page
    assert "ferma il Server Remote Control" in console
    assert "Torna al Progetto" in log
