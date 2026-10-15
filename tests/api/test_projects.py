"""A User creates public Projects from a github.com or gitlab.com URL, lists and deletes them."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dockyard.runtime import ProjectStatus
from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, AccessProxy, wait_until

REPOSITORY = "https://github.com/octocat/Hello-World"
MISSING_REPOSITORY = "https://github.com/octocat/no-such-repository"
OTHER_USER = "another.user@example.com"


@pytest.fixture(autouse=True)
def public_repository(runtime: FakeRuntime) -> None:
    runtime.publish_repository(REPOSITORY)


def create(dockyard: TestClient, headers: dict[str, str], **fields: str) -> Any:
    body = {"name": "Hello", "repository": REPOSITORY, "visibility": "public", **fields}
    return dockyard.post("/api/projects", json=body, headers=headers)


def status_of(dockyard: TestClient, headers: dict[str, str], project: str) -> str | None:
    found = dockyard.get(f"/api/projects/{project}", headers=headers)
    return found.json()["status"] if found.status_code == 200 else None


def test_a_user_creates_a_public_project_that_is_cloned_and_listed(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)

    created = create(dockyard, signed_in, name="Hello World")

    assert created.status_code == 202
    project = created.json()
    assert project["name"] == "Hello World"
    assert project["repository"] == REPOSITORY
    assert project["visibility"] == "public"
    assert wait_until(lambda: status_of(dockyard, signed_in, project["id"]) == "waiting")
    listed = dockyard.get("/api/projects", headers=signed_in).json()
    assert [(p["id"], p["name"], p["status"]) for p in listed] == [
        (project["id"], "Hello World", "waiting")
    ]


@pytest.mark.parametrize(
    ("repository", "error", "message"),
    [
        ("http://github.com/octocat/Hello-World", "repository_not_https", "https://"),
        ("git@github.com:octocat/Hello-World.git", "repository_not_https", "https://"),
        (
            "https://octocat:secret@github.com/octocat/Hello-World",
            "repository_credentials",
            "password",
        ),
        ("https://token@gitlab.com/group/project", "repository_credentials", "password"),
        ("https://github.com:443/octocat/Hello-World", "repository_port", "port"),
        ("https://github.com/octocat/Hello-World?tab=readme", "repository_query", "?"),
        ("https://github.com/octocat/Hello-World#readme", "repository_fragment", "#"),
        (
            "https://bitbucket.org/octocat/Hello-World",
            "repository_host",
            "github.com and gitlab.com",
        ),
        ("https://github.com.evil.example/octocat/Hello-World", "repository_host", "gitlab.com"),
        ("https://gitlab.example.com/group/project", "repository_host", "gitlab.com"),
        ("https://github.com/octocat", "repository_invalid", "https://github.com/"),
        ("https://github.com/octocat/Hello-World/tree/master", "repository_invalid", "owner"),
        ("https://github.com/octocat/../etc", "repository_invalid", "owner"),
        ("", "repository_invalid", "owner"),
    ],
)
def test_repository_urls_other_than_plain_github_or_gitlab_ones_are_refused_with_a_reason(
    dockyard: TestClient, proxy: AccessProxy, repository: str, error: str, message: str
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)

    refused = create(dockyard, signed_in, repository=repository)

    assert refused.status_code == 422
    assert refused.json()["error"] == error
    assert message in refused.json()["message"]
    assert dockyard.get("/api/projects", headers=signed_in).json() == []


@pytest.mark.parametrize(
    "repository",
    [
        "https://github.com/octocat/Hello-World.git",
        "https://GitHub.com/octocat/Hello-World/",
        "https://gitlab.com/gitlab-org/cli",
        "https://gitlab.com/group/subgroup/project",
    ],
)
def test_plain_github_and_gitlab_urls_are_accepted(
    dockyard: TestClient, proxy: AccessProxy, repository: str
) -> None:
    assert create(dockyard, proxy.headers(ADMINISTRATOR), repository=repository).status_code == 202


@pytest.mark.parametrize(
    ("name", "error"), [("", "name_missing"), ("   ", "name_missing"), ("x" * 61, "name_invalid")]
)
def test_a_project_needs_a_name_of_one_line_and_at_most_60_characters(
    dockyard: TestClient, proxy: AccessProxy, name: str, error: str
) -> None:
    refused = create(dockyard, proxy.headers(ADMINISTRATOR), name=name)

    assert refused.status_code == 422
    assert refused.json()["error"] == error


def test_while_a_project_clones_its_user_sees_it_cloning_with_gits_progress(
    dockyard: TestClient, proxy: AccessProxy, runtime: FakeRuntime
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)

    with runtime.clones_paused():
        project = create(dockyard, signed_in).json()
        assert project["status"] == "cloning"
        assert status_of(dockyard, signed_in, project["id"]) == "cloning"
        assert wait_until(
            lambda: "Cloning into" in clone_of(dockyard, signed_in, project["id"])["output"]
        )
        assert clone_of(dockyard, signed_in, project["id"])["state"] == "running"

    assert wait_until(lambda: clone_of(dockyard, signed_in, project["id"])["state"] == "succeeded")
    assert wait_until(lambda: status_of(dockyard, signed_in, project["id"]) == "waiting")


def test_each_project_is_cloned_into_a_path_of_its_own(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)

    first, second = cloned(dockyard, signed_in), cloned(dockyard, signed_in)

    assert first != second
    for project in (first, second):
        assert f"/workspace/{project}" in clone_of(dockyard, signed_in, project)["output"]


def test_a_failed_clone_shows_gits_error_and_leaves_nothing_behind(
    dockyard: TestClient, proxy: AccessProxy, runtime: FakeRuntime
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)

    project = create(dockyard, signed_in, repository=MISSING_REPOSITORY).json()

    assert wait_until(lambda: status_of(dockyard, signed_in, project["id"]) is None)
    clone = clone_of(dockyard, signed_in, project["id"])
    assert clone["state"] == "failed"
    assert "fatal: could not read Username" in clone["output"]
    assert dockyard.get("/api/projects", headers=signed_in).json() == []
    assert runtime.project_status(project["id"]) == ProjectStatus.MISSING
    assert runtime.clone(project["id"]) is None


def test_deleting_a_project_keeps_its_code_unless_asked(
    dockyard: TestClient, proxy: AccessProxy, runtime: FakeRuntime
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    kept = cloned(dockyard, signed_in)
    removed = cloned(dockyard, signed_in)

    assert dockyard.delete(f"/api/projects/{kept}", headers=signed_in).status_code == 204
    deleted = dockyard.delete(f"/api/projects/{removed}?delete_code=true", headers=signed_in)

    assert deleted.status_code == 204
    assert dockyard.get("/api/projects", headers=signed_in).json() == []
    assert runtime.project_status(kept) == ProjectStatus.STOPPED
    assert runtime.project_status(removed) == ProjectStatus.MISSING
    assert dockyard.delete(f"/api/projects/{kept}", headers=signed_in).status_code == 404


def test_deleting_a_project_while_it_clones_stops_the_clone(
    dockyard: TestClient, proxy: AccessProxy, runtime: FakeRuntime
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    with runtime.clones_paused():
        project = create(dockyard, signed_in).json()["id"]

        deleted = dockyard.delete(f"/api/projects/{project}?delete_code=true", headers=signed_in)

    assert deleted.status_code == 204
    assert runtime.clone(project) is None
    assert runtime.project_status(project) == ProjectStatus.MISSING
    assert dockyard.get("/api/projects", headers=signed_in).json() == []


def test_a_user_sees_and_reaches_only_their_own_projects(
    dockyard: TestClient, proxy: AccessProxy, runtime: FakeRuntime, add_user: Callable[[str], None]
) -> None:
    add_user(OTHER_USER)
    project = cloned(dockyard, proxy.headers(ADMINISTRATOR))
    other = proxy.headers(OTHER_USER)

    assert dockyard.get("/api/projects", headers=other).json() == []
    assert dockyard.get(f"/api/projects/{project}", headers=other).status_code == 404
    assert dockyard.get(f"/api/projects/{project}/clone", headers=other).status_code == 404
    assert dockyard.delete(f"/api/projects/{project}", headers=other).status_code == 404
    assert runtime.project_status(project) == ProjectStatus.STOPPED


def test_a_project_is_not_created_when_dockyard_cannot_reach_docker(
    dockyard: TestClient, proxy: AccessProxy, runtime: FakeRuntime
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)

    with runtime.unavailable():
        refused = create(dockyard, signed_in)

    assert refused.status_code == 503
    assert refused.json()["error"] == "runtime_unavailable"
    assert dockyard.get("/api/projects", headers=signed_in).json() == []


def test_a_clone_in_progress_when_dockyard_restarts_is_still_followed(
    start_dockyard: Callable[..., TestClient], proxy: AccessProxy, runtime: FakeRuntime
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    with runtime.clones_paused():
        project = create(start_dockyard(), signed_in, repository=MISSING_REPOSITORY).json()
        restarted = start_dockyard()

    assert wait_until(lambda: status_of(restarted, signed_in, project["id"]) is None)
    assert runtime.project_status(project["id"]) == ProjectStatus.MISSING


def clone_of(dockyard: TestClient, headers: dict[str, str], project: str) -> Any:
    return dockyard.get(f"/api/projects/{project}/clone", headers=headers).json()


def cloned(dockyard: TestClient, headers: dict[str, str]) -> str:
    """A new Project, once cloned (it then waits for the Claude Account)."""
    project: str = create(dockyard, headers).json()["id"]
    assert wait_until(lambda: status_of(dockyard, headers, project) == "waiting")
    return project
