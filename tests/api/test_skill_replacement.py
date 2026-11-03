"""An Administrator replaces a Skill by uploading it again: it keeps its id, its name and the
Projects using it, which pick the new files up when they next start."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, AccessProxy, connect_claude_account
from .test_project_skills import (
    ITALIAN,
    RELEASE,
    REPOSITORY,
    USER,
    project_of,
    running,
    skill_files,
)
from .test_skill_archives import archive
from .test_skills import REVIEW, upload

IMPROVED = """---
name: review
description: Review the branch, tests first.
---

Run the tests, then read the diff.
"""


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
    connect_claude_account(dockyard, proxy.headers(USER))
    return proxy.headers(USER)  # the sign-in's socket changed those


@pytest.fixture
def review(dockyard: TestClient, administrator: dict[str, str]) -> str:
    return str(upload(dockyard, administrator).json()["id"])


@pytest.fixture
def release(dockyard: TestClient, administrator: dict[str, str]) -> str:
    return str(upload(dockyard, administrator, RELEASE, "release.md").json()["id"])


def replace(
    dockyard: TestClient,
    headers: dict[str, str],
    skill: str,
    content: str = IMPROVED,
    file_name: str = "review.md",
) -> Any:
    files = {"file": (file_name, content.encode(), "text/markdown")}
    return dockyard.put(f"/api/skills/{skill}", files=files, headers=headers)


def test_an_administrator_replaces_a_skill_and_it_keeps_its_identity(
    dockyard: TestClient, administrator: dict[str, str], review: str
) -> None:
    before = dockyard.get(f"/api/skills/{review}", headers=administrator).json()

    replaced = replace(dockyard, administrator, review)

    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["id"] == review
    assert replaced.json()["description"] == "Review the branch, tests first."
    assert replaced.json()["files"][0]["content"] == IMPROVED
    assert replaced.json()["updated_at"] >= before["updated_at"]
    listed = dockyard.get("/api/skills", headers=administrator).json()
    assert [(skill["id"], skill["description"]) for skill in listed] == [
        (review, "Review the branch, tests first.")
    ]


def test_a_replacement_declaring_another_name_is_refused(
    dockyard: TestClient, administrator: dict[str, str], review: str
) -> None:
    renamed = replace(dockyard, administrator, review, IMPROVED.replace("name: review", "name: x"))
    from_file_name = replace(dockyard, administrator, review, RELEASE, "release.md")
    italian = replace(dockyard, {**administrator, **ITALIAN}, review, RELEASE, "release.md")

    for refused in (renamed, from_file_name):
        assert refused.status_code == 422
        assert refused.json()["error"] == "name_differs"
        assert "another name" in refused.json()["message"]
    assert "nome diverso" in italian.json()["message"]
    assert (
        dockyard.get(f"/api/skills/{review}", headers=administrator).json()["files"][0]["content"]
        == REVIEW
    )


def test_a_replacement_is_validated_as_an_upload(
    dockyard: TestClient, administrator: dict[str, str], review: str
) -> None:
    refused = replace(dockyard, administrator, review, "---\nname: review\n---\n")

    assert refused.status_code == 422
    assert refused.json()["error"] == "description_missing"


def test_a_skill_is_replaced_by_a_zip_too(
    dockyard: TestClient, administrator: dict[str, str], review: str
) -> None:
    folder = archive(("review/SKILL.md", IMPROVED.encode()), ("review/checklist.md", b"- tests"))
    files = {"file": ("review.zip", folder, "application/zip")}

    replaced = dockyard.put(f"/api/skills/{review}", files=files, headers=administrator)

    assert replaced.status_code == 200, replaced.text
    assert sorted(file["path"] for file in replaced.json()["files"]) == [
        "SKILL.md",
        "checklist.md",
    ]


def test_replacing_an_unknown_skill_is_not_found(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    assert replace(dockyard, administrator, "unknown").status_code == 404


def test_only_administrators_replace_skills(
    dockyard: TestClient, user: dict[str, str], review: str
) -> None:
    refused = replace(dockyard, user, review)

    assert refused.status_code == 403
    assert refused.json()["error"] == "administrator_only"
    assert (
        dockyard.get(f"/api/skills/{review}", headers=user).json()["files"][0]["content"] == REVIEW
    )


def test_a_replaced_skill_reaches_a_running_project_only_after_a_restart(
    dockyard: TestClient,
    administrator: dict[str, str],
    user: dict[str, str],
    runtime: FakeRuntime,
    review: str,
    release: str,
) -> None:
    using = running(dockyard, user, skills=[review])
    other = running(dockyard, user, name="Other", skills=[release])
    stopped = running(dockyard, user, name="Stopped", skills=[review])
    assert dockyard.post(f"/api/projects/{stopped}/stop", headers=user).status_code == 200

    assert replace(dockyard, administrator, review).status_code == 200

    assert project_of(dockyard, user, using)["update_available"] is True
    assert project_of(dockyard, user, other)["update_available"] is False
    assert project_of(dockyard, user, stopped)["update_available"] is False
    assert skill_files(runtime, using) == [("review", "SKILL.md", REVIEW)]
    page = dockyard.get(f"/projects/{using}", headers=user).text
    assert "Restart it to pick them up" in page

    assert dockyard.post(f"/api/projects/{using}/restart", headers=user).status_code == 200

    assert skill_files(runtime, using) == [("review", "SKILL.md", IMPROVED)]
    assert project_of(dockyard, user, using)["update_available"] is False


def test_an_administrator_replaces_a_skill_from_its_page(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str], review: str
) -> None:
    action = f'action="/skills/{review}/replace"'
    assert action in dockyard.get(f"/skills/{review}", headers=administrator).text
    assert (
        "Sostituisci"
        in dockyard.get(f"/skills/{review}", headers={**administrator, **ITALIAN}).text
    )
    assert action not in dockyard.get(f"/skills/{review}", headers=user).text

    files = {"file": ("review.md", IMPROVED.encode(), "text/markdown")}
    replaced = dockyard.post(
        f"/skills/{review}/replace", files=files, headers=administrator, follow_redirects=False
    )

    assert replaced.status_code == 303
    assert replaced.headers["location"] == f"/skills/{review}"
    assert (
        "Run the tests, then read the diff."
        in dockyard.get(f"/skills/{review}", headers=administrator).text
    )
    assert dockyard.post(f"/skills/{review}/replace", files=files, headers=user).status_code == 403


def test_the_page_says_why_a_replacement_is_refused(
    dockyard: TestClient, administrator: dict[str, str], review: str
) -> None:
    files = {"file": ("release.md", RELEASE.encode(), "text/markdown")}

    refused = dockyard.post(f"/skills/{review}/replace", files=files, headers=administrator)

    assert refused.status_code == 422
    assert "declares another name" in refused.text
    assert "Tag, then publish." not in refused.text
