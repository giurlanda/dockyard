"""A User creates private Projects with one of their Git Credentials, or attaches one to a public
Project; the Projects using a Git Credential keep it from being deleted."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, AccessProxy, connect_claude_account, wait_until
from .test_project_following import receive_until

PUBLIC_REPOSITORY = "https://github.com/octocat/Hello-World"
PRIVATE_REPOSITORY = "https://github.com/acme/secret-sauce"
GITLAB_REPOSITORY = "https://gitlab.com/acme/pipelines"
GITHUB_TOKEN = "github_pat_11AAAAAAA0the0private0repository0token"
GITLAB_TOKEN = "glpat-the0gitlab0token"
OTHER_USER = "another.user@example.com"


@pytest.fixture(autouse=True)
def repositories(runtime: FakeRuntime) -> None:
    runtime.publish_repository(PUBLIC_REPOSITORY)
    runtime.publish_repository(PRIVATE_REPOSITORY, token=GITHUB_TOKEN)
    runtime.publish_repository(GITLAB_REPOSITORY, token=GITLAB_TOKEN)


@pytest.fixture
def signed_in(proxy: AccessProxy) -> dict[str, str]:
    return proxy.headers(ADMINISTRATOR)


def add_credential(
    dockyard: TestClient,
    headers: dict[str, str],
    *,
    label: str = "Acme",
    provider: str = "github",
    token: str = GITHUB_TOKEN,
) -> str:
    body = {"label": label, "provider": provider, "token": token}
    added = dockyard.post("/api/git-credentials", json=body, headers=headers)
    assert added.status_code == 201, added.text
    return str(added.json()["id"])


def create(dockyard: TestClient, headers: dict[str, str], **fields: str | None) -> Any:
    body = {"name": "Secret", "repository": PRIVATE_REPOSITORY, "visibility": "private", **fields}
    return dockyard.post("/api/projects", json=body, headers=headers)


def status_of(dockyard: TestClient, headers: dict[str, str], project: str) -> str | None:
    found = dockyard.get(f"/api/projects/{project}", headers=headers)
    return found.json()["status"] if found.status_code == 200 else None


def test_a_private_project_is_cloned_with_the_chosen_git_credential(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    credential = add_credential(dockyard, signed_in)

    created = create(dockyard, signed_in, git_credential=credential)

    assert created.status_code == 202, created.text
    project = created.json()
    assert (project["visibility"], project["git_credential"]) == ("private", credential)
    assert wait_until(lambda: status_of(dockyard, signed_in, project["id"]) == "waiting")


def test_a_private_project_without_a_git_credential_is_refused(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    refused = create(dockyard, signed_in)

    assert refused.status_code == 422
    assert refused.json()["error"] == "git_credential_required"
    assert "Git Credential" in refused.json()["message"]
    assert dockyard.get("/api/projects", headers=signed_in).json() == []


def test_a_public_project_takes_a_git_credential_or_none(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    credential = add_credential(dockyard, signed_in)

    without = create(dockyard, signed_in, repository=PUBLIC_REPOSITORY, visibility="public")
    with_one = create(
        dockyard,
        signed_in,
        repository=PUBLIC_REPOSITORY,
        visibility="public",
        git_credential=credential,
    )

    assert without.status_code == 202
    assert without.json()["git_credential"] is None
    assert with_one.status_code == 202
    assert with_one.json()["git_credential"] == credential


@pytest.mark.parametrize(
    ("repository", "provider", "token"),
    [
        (PRIVATE_REPOSITORY, "gitlab", GITLAB_TOKEN),
        (GITLAB_REPOSITORY, "github", GITHUB_TOKEN),
    ],
)
def test_a_git_credential_of_another_provider_is_refused(
    dockyard: TestClient, signed_in: dict[str, str], repository: str, provider: str, token: str
) -> None:
    credential = add_credential(dockyard, signed_in, provider=provider, token=token)

    refused = create(dockyard, signed_in, repository=repository, git_credential=credential)

    assert refused.status_code == 422
    assert refused.json()["error"] == "git_credential_provider"
    assert dockyard.get("/api/projects", headers=signed_in).json() == []


def test_a_gitlab_project_is_cloned_with_a_gitlab_credential(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    credential = add_credential(dockyard, signed_in, provider="gitlab", token=GITLAB_TOKEN)

    created = create(dockyard, signed_in, repository=GITLAB_REPOSITORY, git_credential=credential)

    assert created.status_code == 202, created.text
    assert wait_until(lambda: status_of(dockyard, signed_in, created.json()["id"]) == "waiting")


@pytest.mark.parametrize("credential", ["no-such-credential", "theirs"])
def test_only_the_users_own_git_credentials_are_accepted(
    dockyard: TestClient,
    proxy: AccessProxy,
    signed_in: dict[str, str],
    add_user: Callable[[str], None],
    credential: str,
) -> None:
    add_user(OTHER_USER)
    if credential == "theirs":
        credential = add_credential(dockyard, proxy.headers(OTHER_USER))

    refused = create(dockyard, signed_in, git_credential=credential)

    assert refused.status_code == 422
    assert refused.json()["error"] == "git_credential_not_found"
    assert dockyard.get("/api/projects", headers=signed_in).json() == []


def test_a_wrong_token_makes_the_clone_fail_with_gits_error(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    credential = add_credential(dockyard, signed_in, token="github_pat_11revoked0token")

    project = create(dockyard, signed_in, git_credential=credential).json()["id"]

    assert wait_until(lambda: status_of(dockyard, signed_in, project) is None)
    clone = dockyard.get(f"/api/projects/{project}/clone", headers=signed_in).json()
    assert clone["state"] == "failed"
    assert "Authentication failed" in clone["output"]
    assert "github_pat_11revoked0token" not in clone["output"]


def test_deleting_a_git_credential_in_use_is_refused_and_names_the_projects_using_it(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    credential = add_credential(dockyard, signed_in)
    create(dockyard, signed_in, name="Secret sauce", git_credential=credential)
    create(
        dockyard,
        signed_in,
        name="Hello",
        repository=PUBLIC_REPOSITORY,
        visibility="public",
        git_credential=credential,
    )

    refused = dockyard.delete(f"/api/git-credentials/{credential}", headers=signed_in)

    assert refused.status_code == 409
    assert refused.json()["error"] == "in_use"
    assert "Secret sauce" in refused.json()["message"]
    assert "Hello" in refused.json()["message"]
    assert dockyard.get(f"/api/git-credentials/{credential}", headers=signed_in).status_code == 200


def test_a_git_credential_can_be_deleted_once_its_projects_are_deleted(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    credential = add_credential(dockyard, signed_in)
    project = create(dockyard, signed_in, git_credential=credential).json()["id"]
    assert wait_until(lambda: status_of(dockyard, signed_in, project) == "waiting")

    dockyard.delete(f"/api/projects/{project}", headers=signed_in)

    deleted = dockyard.delete(f"/api/git-credentials/{credential}", headers=signed_in)
    assert deleted.status_code == 204


GH_TOKEN = "gh auth token --hostname github.com"
GLAB_TOKEN = "glab config get token --host gitlab.com"


def running(dockyard: TestClient, headers: dict[str, str], **fields: str) -> str:
    """A new Project of the signed-in User, once its Remote Control Server runs."""
    connect_claude_account(dockyard, headers)
    created = create(dockyard, headers, **fields)
    assert created.status_code == 202, created.text
    project: str = created.json()["id"]
    assert wait_until(lambda: status_of(dockyard, headers, project) == "running")
    return project


def typed(dockyard: TestClient, headers: dict[str, str], project: str, command: str) -> str:
    """What `command`, typed in a new Terminal of the Project, printed."""
    with dockyard.websocket_connect(f"/ws/projects/{project}/terminal", headers=headers) as shell:
        receive_until(shell, "$ ")
        shell.send_json({"type": "input", "data": f"{command}\r"})
        shown = receive_until(shell, "$ ")
    return shown.removeprefix(f"{command}\r\n").rsplit("\r\n", 1)[0]


def test_gh_and_git_inside_a_running_project_use_its_git_credential(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    credential = add_credential(dockyard, signed_in)

    project = running(dockyard, signed_in, git_credential=credential)

    assert typed(dockyard, signed_in, project, GH_TOKEN) == GITHUB_TOKEN


def test_glab_inside_a_running_gitlab_project_uses_its_git_credential(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    credential = add_credential(dockyard, signed_in, provider="gitlab", token=GITLAB_TOKEN)

    project = running(dockyard, signed_in, repository=GITLAB_REPOSITORY, git_credential=credential)

    assert typed(dockyard, signed_in, project, GLAB_TOKEN) == GITLAB_TOKEN


def test_a_public_project_without_a_git_credential_has_none_inside(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    project = running(dockyard, signed_in, repository=PUBLIC_REPOSITORY, visibility="public")

    assert typed(dockyard, signed_in, project, GH_TOKEN) == "no oauth token found for github.com"


def test_a_rotated_token_reaches_the_project_at_its_next_start(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    credential = add_credential(dockyard, signed_in)
    project = running(dockyard, signed_in, git_credential=credential)
    rotated = "github_pat_11the0token0after0rotation"

    dockyard.patch(f"/api/git-credentials/{credential}", json={"token": rotated}, headers=signed_in)

    assert typed(dockyard, signed_in, project, GH_TOKEN) == GITHUB_TOKEN
    dockyard.post(f"/api/projects/{project}/restart", headers=signed_in)
    assert typed(dockyard, signed_in, project, GH_TOKEN) == rotated


def test_a_project_restarted_by_itself_gets_its_git_credential_again(
    dockyard: TestClient, signed_in: dict[str, str], runtime: FakeRuntime
) -> None:
    """While Dockyard runs, as it does behind its web server."""
    with dockyard:
        credential = add_credential(dockyard, signed_in)
        project = running(dockyard, signed_in, git_credential=credential)

        runtime.exit_server(project)

        assert wait_until(lambda: typed(dockyard, signed_in, project, GH_TOKEN) == GITHUB_TOKEN)
