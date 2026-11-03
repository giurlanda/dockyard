"""A User picks Skills when creating a Project or later: the Project lists them, its environment
gets them at every start, a running one says a restart is needed after a change, and a Skill a
Project uses cannot be deleted."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, AccessProxy, connect_claude_account, wait_until
from .test_images import built
from .test_skills import REVIEW, upload

REPOSITORY = "https://github.com/octocat/Hello-World"
USER = "a.user@example.com"
OTHER_USER = "another.user@example.com"
ITALIAN = {"Accept-Language": "it-IT,it;q=0.9"}
RELEASE = "---\ndescription: Cut a release.\n---\nTag, then publish.\n"


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


@pytest.fixture
def review(dockyard: TestClient, administrator: dict[str, str]) -> str:
    return str(upload(dockyard, administrator).json()["id"])


@pytest.fixture
def release(dockyard: TestClient, administrator: dict[str, str]) -> str:
    return str(upload(dockyard, administrator, RELEASE, "release.md").json()["id"])


def create(dockyard: TestClient, headers: dict[str, str], **fields: Any) -> Any:
    body = {"name": "Hello", "repository": REPOSITORY, "visibility": "public", **fields}
    return dockyard.post("/api/projects", json=body, headers=headers)


def project_of(dockyard: TestClient, headers: dict[str, str], project: str) -> Any:
    return dockyard.get(f"/api/projects/{project}", headers=headers).json()


def running(dockyard: TestClient, headers: dict[str, str], **fields: Any) -> str:
    created = create(dockyard, headers, **fields)
    assert created.status_code == 202, created.text
    project = str(created.json()["id"])
    assert wait_until(lambda: project_of(dockyard, headers, project)["status"] == "running")
    return project


def skill_files(runtime: FakeRuntime, project: str) -> list[tuple[str, str, str]]:
    """What the Project's environment last started with: each Skill's files, by path."""
    return sorted(
        (skill.name, file.path, file.content.decode())
        for skill in runtime.skills(project) or ()
        for file in skill.files
    )


def test_a_project_created_with_skills_lists_them(
    dockyard: TestClient, user: dict[str, str], review: str, release: str
) -> None:
    created = create(dockyard, user, skills=[review, release])

    assert created.status_code == 202, created.text
    shown = project_of(dockyard, user, created.json()["id"])
    assert [(s["id"], s["name"], s["description"]) for s in shown["skills"]] == [
        (release, "release", "Cut a release."),
        (review, "review", "Review the current branch against the team's checklist."),
    ]
    assert created.json()["skills"] == shown["skills"]


def test_its_environment_gets_exactly_its_skills_at_every_start(
    dockyard: TestClient, user: dict[str, str], runtime: FakeRuntime, review: str, release: str
) -> None:
    project = running(dockyard, user, skills=[review])
    expected = [("review", "SKILL.md", REVIEW)]
    assert skill_files(runtime, project) == expected

    first_start = runtime.project_starts()[project]

    assert dockyard.post(f"/api/projects/{project}/restart", headers=user).status_code == 200

    assert runtime.project_starts()[project] != first_start
    assert skill_files(runtime, project) == expected


def test_a_project_without_skills_starts_with_none(
    dockyard: TestClient, user: dict[str, str], runtime: FakeRuntime, review: str
) -> None:
    project = running(dockyard, user)

    assert project_of(dockyard, user, project)["skills"] == []
    assert runtime.skills(project) == ()


def test_skills_go_with_any_image(
    dockyard: TestClient,
    administrator: dict[str, str],
    user: dict[str, str],
    runtime: FakeRuntime,
    review: str,
) -> None:
    image = built(dockyard, administrator)["id"]

    project = running(dockyard, user, image=image, skills=[review])

    assert project_of(dockyard, user, project)["image"] == image
    assert skill_files(runtime, project) == [("review", "SKILL.md", REVIEW)]


def test_creation_with_a_skill_that_does_not_exist_is_refused(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str], review: str
) -> None:
    assert dockyard.delete(f"/api/skills/{review}", headers=administrator).status_code == 204

    refused = create(dockyard, user, skills=[review])

    assert refused.status_code == 422
    assert refused.json()["error"] == "skill_not_found"
    assert "Skill" in refused.json()["message"]
    assert dockyard.get("/api/projects", headers=user).json() == []


def test_a_skill_projects_use_cannot_be_deleted_and_the_refusal_names_them(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str], review: str
) -> None:
    create(dockyard, user, name="Rusty", skills=[review])
    create(dockyard, administrator, name="Mine", skills=[review])

    refused = dockyard.delete(f"/api/skills/{review}", headers=administrator)
    from_page = dockyard.post(f"/skills/{review}/delete", headers=administrator)

    assert refused.status_code == 409
    assert refused.json()["error"] == "in_use"
    assert f"Rusty ({USER})" in refused.json()["message"]
    assert f"Mine ({ADMINISTRATOR})" in refused.json()["message"]
    assert from_page.status_code == 409
    assert "the Projects listed below use it" in from_page.text
    assert "Rusty" in from_page.text
    assert dockyard.get(f"/api/skills/{review}", headers=user).status_code == 200


def test_an_administrator_sees_which_projects_use_a_skill_and_whose_they_are(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str], review: str
) -> None:
    create(dockyard, user, name="Rusty", skills=[review])

    shown = dockyard.get(f"/api/skills/{review}", headers=administrator).json()
    page = dockyard.get(f"/skills/{review}", headers=administrator).text

    assert shown["used_by"] == [{"name": "Rusty", "owner": USER}]
    assert "Rusty" in page
    assert USER in page
    assert f"/skills/{review}/delete" not in page


def test_users_who_are_not_administrators_do_not_see_which_projects_use_a_skill(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str], review: str
) -> None:
    create(dockyard, administrator, name="Mine", skills=[review])

    shown = dockyard.get(f"/api/skills/{review}", headers=user).json()
    page = dockyard.get(f"/skills/{review}", headers=user).text

    assert "used_by" not in shown
    assert "Mine" not in page
    assert ADMINISTRATOR not in page


def test_a_skill_is_free_once_its_last_project_drops_it(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str], review: str
) -> None:
    first = create(dockyard, user, name="First", skills=[review]).json()["id"]
    last = create(dockyard, user, name="Last", skills=[review]).json()["id"]

    assert change_skills(dockyard, user, first, []).status_code == 200

    assert dockyard.delete(f"/api/skills/{review}", headers=administrator).status_code == 409

    assert change_skills(dockyard, user, last, []).status_code == 200

    assert dockyard.get(f"/api/skills/{review}", headers=administrator).json()["used_by"] == []
    assert dockyard.delete(f"/api/skills/{review}", headers=administrator).status_code == 204


def test_a_skill_is_free_once_its_last_project_is_deleted(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str], review: str
) -> None:
    project = create(dockyard, user, skills=[review]).json()["id"]

    dockyard.delete(f"/api/projects/{project}", headers=user)

    assert dockyard.delete(f"/api/skills/{review}", headers=administrator).status_code == 204


def test_a_skill_is_free_once_the_owner_of_its_last_project_is_removed(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str], review: str
) -> None:
    create(dockyard, user, skills=[review])

    assert dockyard.delete(f"/api/users/{USER}", headers=administrator).status_code == 204

    assert dockyard.get(f"/api/skills/{review}", headers=administrator).json()["used_by"] == []
    assert dockyard.delete(f"/api/skills/{review}", headers=administrator).status_code == 204


def test_the_new_project_form_offers_every_skill_none_picked_with_a_link_to_read_it(
    dockyard: TestClient, user: dict[str, str], review: str
) -> None:
    for headers, label in ((user, "Skills"), ({**user, **ITALIAN}, "Skill")):
        page = dockyard.get("/projects/new", headers=headers)

        assert page.status_code == 200
        assert f"<legend>{label}</legend>" in page.text
        assert f'name="skills" value="{review}"' in page.text
        assert f'value="{review}" checked' not in page.text
        assert "Review the current branch against the team&#39;s checklist." in page.text
        assert f'href="/skills/{review}"' in page.text
    assert dockyard.get(f"/skills/{review}", headers=user).status_code == 200


def test_skills_picked_on_the_form_are_listed_on_the_project_page(
    dockyard: TestClient, user: dict[str, str], review: str, release: str
) -> None:
    form = {"name": "Hello", "repository": REPOSITORY, "visibility": "public", "skills": [review]}

    created = dockyard.post("/projects", data=form, headers=user, follow_redirects=False)

    assert created.status_code == 303
    page = dockyard.get(created.headers["location"], headers=user)
    assert f'<li><a href="/skills/{review}">review</a>' in page.text
    assert f'<li><a href="/skills/{release}">' not in page.text


def test_trying_again_after_a_failed_clone_keeps_the_picked_skills(
    dockyard: TestClient, user: dict[str, str], review: str, release: str
) -> None:
    missing = "https://github.com/octocat/no-such-repository"
    created = create(dockyard, user, repository=missing, skills=[review])
    path = f"/projects/{created.json()['id']}"
    assert wait_until(lambda: "The clone failed" in dockyard.get(path, headers=user).text)

    retry = dockyard.get(path, headers=user).text.split('href="/projects/new?')[1].split('"')[0]
    form = dockyard.get(f"/projects/new?{retry.replace('&amp;', '&')}", headers=user).text

    assert f'value="{review}" checked' in form
    assert f'value="{release}" checked' not in form


def test_the_form_keeps_the_picked_skills_when_creation_is_refused(
    dockyard: TestClient, user: dict[str, str], review: str
) -> None:
    form = {"name": "", "repository": REPOSITORY, "visibility": "public", "skills": [review]}

    refused = dockyard.post("/projects", data=form, headers=user)

    assert refused.status_code == 422
    assert f'value="{review}" checked' in refused.text


def change_skills(
    dockyard: TestClient, headers: dict[str, str], project: str, skills: list[str]
) -> Any:
    return dockyard.put(f"/api/projects/{project}/skills", json=skills, headers=headers)


def test_skills_changed_on_a_running_project_reach_it_only_after_a_restart(
    dockyard: TestClient, user: dict[str, str], runtime: FakeRuntime, review: str, release: str
) -> None:
    project = running(dockyard, user, skills=[review])
    assert project_of(dockyard, user, project)["update_available"] is False

    changed = change_skills(dockyard, user, project, [release])

    assert changed.status_code == 200, changed.text
    assert [skill["name"] for skill in changed.json()["skills"]] == ["release"]
    assert changed.json()["update_available"] is True
    assert project_of(dockyard, user, project)["status"] == "running"
    assert skill_files(runtime, project) == [("review", "SKILL.md", REVIEW)]

    assert dockyard.post(f"/api/projects/{project}/restart", headers=user).status_code == 200

    assert skill_files(runtime, project) == [("release", "SKILL.md", RELEASE)]
    assert project_of(dockyard, user, project)["update_available"] is False


def test_skills_changed_on_a_stopped_project_reach_it_at_its_next_start_with_no_notice(
    dockyard: TestClient, user: dict[str, str], runtime: FakeRuntime, review: str, release: str
) -> None:
    project = running(dockyard, user, skills=[review])
    assert dockyard.post(f"/api/projects/{project}/stop", headers=user).status_code == 200

    changed = change_skills(dockyard, user, project, [review, release])

    assert changed.status_code == 200, changed.text
    assert [skill["name"] for skill in changed.json()["skills"]] == ["release", "review"]
    assert changed.json()["update_available"] is False

    assert dockyard.post(f"/api/projects/{project}/start", headers=user).status_code == 200

    assert skill_files(runtime, project) == [
        ("release", "SKILL.md", RELEASE),
        ("review", "SKILL.md", REVIEW),
    ]
    assert project_of(dockyard, user, project)["update_available"] is False


def test_changing_to_a_skill_that_does_not_exist_is_refused(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str], review: str
) -> None:
    project = running(dockyard, user)
    assert dockyard.delete(f"/api/skills/{review}", headers=administrator).status_code == 204

    refused = change_skills(dockyard, user, project, [review])

    assert refused.status_code == 422
    assert refused.json()["error"] == "skill_not_found"
    assert project_of(dockyard, user, project)["skills"] == []
    assert project_of(dockyard, user, project)["update_available"] is False


def test_only_its_owner_changes_a_projects_skills(
    dockyard: TestClient,
    proxy: AccessProxy,
    add_user: Callable[[str], None],
    user: dict[str, str],
    review: str,
) -> None:
    project = running(dockyard, user)
    add_user(OTHER_USER)
    other = proxy.headers(OTHER_USER)
    administrator = proxy.headers(ADMINISTRATOR)

    for headers in (other, administrator):
        assert change_skills(dockyard, headers, project, [review]).status_code == 404
    assert project_of(dockyard, user, project)["skills"] == []


def test_the_project_page_changes_its_skills_and_says_a_restart_is_needed_while_it_runs(
    dockyard: TestClient, user: dict[str, str], runtime: FakeRuntime, review: str, release: str
) -> None:
    project = running(dockyard, user, skills=[review])
    for headers, label in ((user, "Change Skills"), ({**user, **ITALIAN}, "Cambia Skill")):
        page = dockyard.get(f"/projects/{project}", headers=headers).text
        assert f'<h2 id="skills">{label}</h2>' in page
        assert f'value="{review}" checked' in page
        assert f'value="{release}" checked' not in page
        assert "Restart it" not in page and "Riavvialo" not in page

    changed = dockyard.post(
        f"/projects/{project}/skills",
        data={"skills": [release]},
        headers=user,
        follow_redirects=False,
    )

    assert changed.status_code == 303
    page = dockyard.get(changed.headers["location"], headers=user).text
    assert f'value="{release}" checked' in page
    assert "Restart it to pick them up" in page
    assert skill_files(runtime, project) == [("review", "SKILL.md", REVIEW)]


def test_unpicking_every_skill_on_the_page_leaves_the_project_with_none(
    dockyard: TestClient, user: dict[str, str], review: str
) -> None:
    project = running(dockyard, user, skills=[review])

    changed = dockyard.post(f"/projects/{project}/skills", headers=user, follow_redirects=False)

    assert changed.status_code == 303
    assert project_of(dockyard, user, project)["skills"] == []


def test_the_page_says_why_a_skill_change_is_refused(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str], review: str
) -> None:
    project = running(dockyard, user)
    assert dockyard.delete(f"/api/skills/{review}", headers=administrator).status_code == 204

    refused = dockyard.post(f"/projects/{project}/skills", data={"skills": [review]}, headers=user)

    assert refused.status_code == 422
    assert "no longer exists" in refused.text
