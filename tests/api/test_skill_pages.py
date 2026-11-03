"""The pages where an Administrator uploads and deletes Skills, and where every User reads one."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from .conftest import ADMINISTRATOR, AccessProxy

USER = "a.user@example.com"
ITALIAN = {"Accept-Language": "it-IT,it;q=0.9"}
REVIEW = """---
name: review
description: Review the current branch against the team's checklist.
---

Run `git diff main` & check <every> item.
"""


@pytest.fixture
def administrator(proxy: AccessProxy) -> dict[str, str]:
    return proxy.headers(ADMINISTRATOR)


@pytest.fixture
def user(proxy: AccessProxy, add_user: Callable[[str], None]) -> dict[str, str]:
    add_user(USER)
    return proxy.headers(USER)


def submit(
    dockyard: TestClient,
    headers: dict[str, str],
    content: str = REVIEW,
    file_name: str = "review.md",
) -> Response:
    files = {"file": (file_name, content.encode(), "text/markdown")}
    return dockyard.post("/skills", files=files, headers=headers, follow_redirects=False)


def uploaded(dockyard: TestClient, headers: dict[str, str]) -> str:
    """A new Skill, by its id."""
    return submit(dockyard, headers).headers["location"].removeprefix("/skills/")


def test_the_dashboard_leads_administrators_only_to_the_skills(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    assert 'href="/skills"' in dockyard.get("/", headers=administrator).text
    assert 'href="/skills"' not in dockyard.get("/", headers=user).text


def test_an_administrator_uploads_a_skill_and_reads_it_on_its_page(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    form = dockyard.get("/skills", headers=administrator)
    assert 'type="file"' in form.text
    assert 'enctype="multipart/form-data"' in form.text

    created = submit(dockyard, administrator)

    assert created.status_code == 303
    page = dockyard.get(created.headers["location"], headers=administrator)
    assert "review" in page.text
    assert "Review the current branch against the team&#39;s checklist." in page.text
    assert "Run `git diff main` &amp; check &lt;every&gt; item." in page.text  # never as HTML
    listed = dockyard.get("/skills", headers=administrator).text
    assert f'href="{created.headers["location"]}"' in listed
    assert "Review the current branch" in listed
    assert "<time" in listed


def test_a_refused_upload_says_why_on_the_page(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    refused = submit(dockyard, administrator, "# No frontmatter\n")
    submit(dockyard, administrator)
    taken = submit(dockyard, administrator)

    assert refused.status_code == 422
    assert "Add a description to the frontmatter" in refused.text
    assert taken.status_code == 409
    assert "Another Skill already has this name" in taken.text


def test_every_user_reads_a_skill_but_only_administrators_manage_them(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    skill = uploaded(dockyard, administrator)

    read = dockyard.get(f"/skills/{skill}", headers=user)

    assert read.status_code == 200
    assert "Run `git diff main`" in read.text
    assert f'action="/skills/{skill}/delete"' not in read.text
    assert (
        f'action="/skills/{skill}/delete"'
        in dockyard.get(f"/skills/{skill}", headers=administrator).text
    )
    assert dockyard.get("/skills", headers=user).status_code == 403
    assert submit(dockyard, user).status_code == 403
    assert dockyard.post(f"/skills/{skill}/delete", headers=user).status_code == 403


def test_an_administrator_deletes_a_skill_from_its_page(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    skill = uploaded(dockyard, administrator)

    deleted = dockyard.post(
        f"/skills/{skill}/delete", headers=administrator, follow_redirects=False
    )

    assert deleted.status_code == 303
    assert deleted.headers["location"] == "/skills"
    assert f"/skills/{skill}" not in dockyard.get("/skills", headers=administrator).text
    assert dockyard.get(f"/skills/{skill}", headers=administrator).status_code == 404


def test_the_skill_pages_are_in_italian_too(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    skill = uploaded(dockyard, administrator)

    assert "Carica una Skill" in dockyard.get("/skills", headers={**administrator, **ITALIAN}).text
    assert (
        "Elimina Skill"
        in dockyard.get(f"/skills/{skill}", headers={**administrator, **ITALIAN}).text
    )
    assert ">Skill</a>" in dockyard.get("/", headers={**administrator, **ITALIAN}).text
