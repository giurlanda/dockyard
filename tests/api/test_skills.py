"""Skills: an Administrator uploads a Markdown file as a Skill; Dockyard reads its name and
description, keeps it in the catalog, and every User reads it before picking it."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from .conftest import ADMINISTRATOR, AccessProxy

USER = "a.user@example.com"
ITALIAN = {"Accept-Language": "it-IT,it;q=0.9"}
REVIEW = """---
name: review
description: Review the current branch against the team's checklist.
---

# Review

Read the diff, then check every item of the list.
"""


@pytest.fixture
def administrator(proxy: AccessProxy) -> dict[str, str]:
    return proxy.headers(ADMINISTRATOR)


@pytest.fixture
def user(proxy: AccessProxy, add_user: Callable[[str], None]) -> dict[str, str]:
    add_user(USER)
    return proxy.headers(USER)


def upload(
    dockyard: TestClient,
    headers: dict[str, str],
    content: str | bytes = REVIEW,
    file_name: str = "review.md",
) -> Any:
    data = content.encode() if isinstance(content, str) else content
    files = {"file": (file_name, data, "text/markdown")}
    return dockyard.post("/api/skills", files=files, headers=headers)


def test_an_administrator_uploads_a_markdown_file_and_it_is_listed(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    uploaded = upload(dockyard, administrator)

    assert uploaded.status_code == 201
    listed = dockyard.get("/api/skills", headers=administrator).json()
    assert [(skill["name"], skill["description"]) for skill in listed] == [
        ("review", "Review the current branch against the team's checklist.")
    ]
    assert listed[0]["id"] == uploaded.json()["id"]
    assert listed[0]["updated_at"].startswith("20")


def test_a_markdown_file_without_a_name_takes_it_from_the_file_name(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    content = "---\ndescription: Cut a release.\n---\nTag, then publish.\n"

    uploaded = upload(dockyard, administrator, content, file_name="release-2.md")

    assert uploaded.status_code == 201
    assert uploaded.json()["name"] == "release-2"
    empty_name = "---\nname:\ndescription: Deploy.\n---\n"
    assert upload(dockyard, administrator, empty_name, "deploy.md").json()["name"] == "deploy"


@pytest.mark.parametrize(
    ("content", "file_name", "reason"),
    [
        ("---\ndescription: Too big.\n---\n" + "x" * 1024 * 1024, "big.md", "too_large"),
        ("# Review\n\nNo frontmatter at all.\n", "review.md", "description_missing"),
        ("---\nname: review\ndescription: '  '\n---\n", "review.md", "description_missing"),
        ("---\ndescription: " + "x" * 1025 + "\n---\n", "review.md", "description_invalid"),
        ("---\nname: Review\ndescription: Upper case.\n---\n", "review.md", "name_invalid"),
        ("---\ndescription: Spaces.\n---\n", "my review.md", "name_invalid"),
        ("---\nname: " + "a" * 65 + "\ndescription: Long.\n---\n", "a.md", "name_invalid"),
        ("---\ndescription: [unclosed\n---\n", "review.md", "frontmatter_invalid"),
        ("---\ndescription: Never closed.\n", "review.md", "frontmatter_invalid"),
        (b"---\ndescription: \xff\n---\n", "review.md", "not_text"),
        ("description: A text file.", "review.txt", "not_markdown"),
    ],
)
def test_a_malformed_skill_is_refused_with_a_reason(
    dockyard: TestClient,
    administrator: dict[str, str],
    content: str | bytes,
    file_name: str,
    reason: str,
) -> None:
    refused = upload(dockyard, administrator, content, file_name)

    assert refused.status_code == 422
    assert refused.json()["error"] == reason
    assert refused.json()["message"]
    assert dockyard.get("/api/skills", headers=administrator).json() == []


def test_the_reason_of_a_refusal_is_in_the_users_language(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    refused = upload(dockyard, administrator, "# No description\n", "review.md")
    italian = upload(dockyard, {**administrator, **ITALIAN}, "# No description\n", "review.md")

    assert "description" in refused.json()["message"]
    assert "descrizione" in italian.json()["message"]


def test_a_second_skill_with_the_same_name_is_refused_as_a_conflict(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    upload(dockyard, administrator)

    again = upload(dockyard, administrator, "---\ndescription: Another.\n---\n", "review.md")

    assert again.status_code == 409
    assert again.json()["error"] == "name_taken"
    assert len(dockyard.get("/api/skills", headers=administrator).json()) == 1


def test_every_user_lists_skills_and_reads_one_with_its_full_content(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    skill = upload(dockyard, administrator).json()["id"]

    listed = dockyard.get("/api/skills", headers=user).json()
    read = dockyard.get(f"/api/skills/{skill}", headers=user)

    assert [entry["name"] for entry in listed] == ["review"]
    assert read.status_code == 200
    assert read.json()["description"] == "Review the current branch against the team's checklist."
    assert read.json()["files"] == [
        {"path": "SKILL.md", "size": len(REVIEW.encode()), "content": REVIEW}
    ]
    assert dockyard.get("/api/skills/unknown", headers=user).status_code == 404


def test_only_administrators_upload_and_delete_skills(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    refused = upload(dockyard, user)
    skill = upload(dockyard, administrator).json()["id"]
    not_deleted = dockyard.delete(f"/api/skills/{skill}", headers=user)

    assert refused.status_code == 403
    assert refused.json()["error"] == "administrator_only"
    assert not_deleted.status_code == 403
    assert [entry["name"] for entry in dockyard.get("/api/skills", headers=user).json()] == [
        "review"
    ]


def test_an_administrator_deletes_a_skill_and_it_leaves_the_list(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    skill = upload(dockyard, administrator).json()["id"]

    deleted = dockyard.delete(f"/api/skills/{skill}", headers=administrator)

    assert deleted.status_code == 204
    assert dockyard.get("/api/skills", headers=administrator).json() == []
    assert dockyard.get(f"/api/skills/{skill}", headers=administrator).status_code == 404
    assert dockyard.delete(f"/api/skills/{skill}", headers=administrator).status_code == 404
