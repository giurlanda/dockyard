"""Images: an Administrator defines one by a label, a description and only its extra build
instructions; Dockyard builds it on top of the Base Image and checks it before Users may pick it."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, AccessProxy, connect_claude_account, wait_until
from .test_project_following import receive_until

USER = "a.user@example.com"
INSTRUCTIONS = "RUN echo rust-toolchain > /etc/rust"
REPOSITORY = "https://github.com/octocat/Hello-World"


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


def create_image(dockyard: TestClient, headers: dict[str, str], **fields: str) -> Any:
    body = {
        "label": "Rust",
        "description": "The Rust toolchain, on top of the Base Image.",
        "instructions": INSTRUCTIONS,
        **fields,
    }
    return dockyard.post("/api/images", json=body, headers=headers)


def shown(dockyard: TestClient, headers: dict[str, str], image: str) -> Any:
    return dockyard.get(f"/api/images/{image}", headers=headers).json()


def create_project(dockyard: TestClient, headers: dict[str, str], **fields: str) -> Any:
    body = {"name": "Hello", "repository": REPOSITORY, "visibility": "public", **fields}
    return dockyard.post("/api/projects", json=body, headers=headers)


def terminal_shows(dockyard: TestClient, headers: dict[str, str], project: str, typed: str) -> str:
    """What the running Project's Terminal prints for a command."""
    assert wait_until(
        lambda: (
            dockyard.get(f"/api/projects/{project}", headers=headers).json()["status"] == "running"
        )
    )
    with dockyard.websocket_connect(f"/ws/projects/{project}/terminal", headers=headers) as shell:
        receive_until(shell, "$ ")
        shell.send_json({"type": "input", "data": f"{typed}\r"})
        return receive_until(shell, "\r\n" + "ubuntu@")


def built(dockyard: TestClient, headers: dict[str, str], **fields: str) -> Any:
    """A new Image, once its build and check have ended."""
    image = create_image(dockyard, headers, **fields).json()["id"]
    assert wait_until(lambda: shown(dockyard, headers, image)["status"] != "building")
    return shown(dockyard, headers, image)


def test_an_administrator_creates_an_image_that_is_built_then_ready(
    dockyard: TestClient, administrator: dict[str, str], runtime: FakeRuntime
) -> None:
    with runtime.builds_paused():
        created = create_image(dockyard, administrator)

        assert created.status_code == 202
        image = created.json()["id"]
        assert created.json()["status"] == "building"
        assert wait_until(lambda: "Step 1/4 : FROM" in shown(dockyard, administrator, image)["log"])
        assert shown(dockyard, administrator, image)["status"] == "building"

    assert wait_until(lambda: shown(dockyard, administrator, image)["status"] == "ready")
    image_shown = shown(dockyard, administrator, image)
    assert image_shown["label"] == "Rust"
    assert image_shown["description"] == "The Rust toolchain, on top of the Base Image."
    assert image_shown["instructions"] == INSTRUCTIONS
    # Wrapped: from the Base Image, the instructions as root, then back to the non-root user.
    log = image_shown["log"]
    assert log.index("FROM") < log.index("USER root") < log.index(INSTRUCTIONS)
    assert log.index(INSTRUCTIONS) < log.index("USER ubuntu")
    assert image_shown["failure"] is None


def test_only_administrators_create_and_follow_images(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    refused = create_image(dockyard, user)
    image = create_image(dockyard, administrator).json()["id"]

    assert refused.status_code == 403
    assert refused.json()["error"] == "administrator_only"
    assert dockyard.get(f"/api/images/{image}", headers=user).status_code == 403
    assert dockyard.delete(f"/api/images/{image}", headers=user).status_code == 403


def test_a_failing_instruction_fails_the_image_and_its_log_says_why(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    image = built(dockyard, administrator, instructions="RUN true\nRUN false")

    assert image["status"] == "failed"
    assert image["failure"] == "build"
    assert "returned a non-zero code: 1" in image["log"]


@pytest.mark.parametrize(
    ("instructions", "failure"),
    [
        ("RUN rm /usr/local/bin/claude", "claude_missing"),
        ("RUN usermod --non-unique --uid 0 ubuntu", "root_user"),
    ],
)
def test_a_built_image_that_breaks_what_projects_need_is_not_ready(
    dockyard: TestClient, administrator: dict[str, str], instructions: str, failure: str
) -> None:
    image = built(dockyard, administrator, instructions=instructions)

    assert image["status"] == "failed"
    assert image["failure"] == failure


def test_users_see_the_label_and_description_of_ready_images_only(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str], runtime: FakeRuntime
) -> None:
    ready = built(dockyard, administrator)
    failed = built(dockyard, administrator, label="Broken", instructions="RUN false")
    with runtime.builds_paused():
        building = create_image(dockyard, administrator, label="Slow").json()

        offered = dockyard.get("/api/images", headers=user).json()
        every_one = dockyard.get("/api/images", headers=administrator).json()

    assert [image["label"] for image in offered] == ["Base Image", "Rust"]
    assert offered[0]["base"] and offered[0]["id"] == "base"
    assert offered[1] == {
        "id": ready["id"],
        "label": "Rust",
        "description": "The Rust toolchain, on top of the Base Image.",
        "base": False,
        "status": "ready",
    }
    assert "log" not in offered[1] and "instructions" not in offered[1]
    assert [(image["id"], image["status"]) for image in every_one] == [
        ("base", "ready"),
        (ready["id"], "ready"),
        (failed["id"], "failed"),
        (building["id"], "building"),
    ]


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"label": "  "}, "label_missing"),
        ({"label": "x" * 61}, "label_invalid"),
        ({"description": "x" * 501}, "description_invalid"),
        ({"instructions": "\n"}, "instructions_missing"),
        ({"instructions": "RUN true\nFROM ubuntu:24.04"}, "instructions_from"),
    ],
)
def test_an_image_is_refused_with_a_reason(
    dockyard: TestClient, administrator: dict[str, str], fields: dict[str, str], error: str
) -> None:
    refused = create_image(dockyard, administrator, **fields)

    assert refused.status_code == 422
    assert refused.json()["error"] == error
    assert refused.json()["message"]
    assert len(dockyard.get("/api/images", headers=administrator).json()) == 1  # the Base Image


def test_the_base_image_cannot_be_deleted(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    refused = dockyard.delete("/api/images/base", headers=administrator)

    assert refused.status_code == 409
    assert refused.json()["error"] == "base_image"
    assert dockyard.get("/api/images/base", headers=administrator).json()["status"] == "ready"


def test_an_unused_image_is_deleted(dockyard: TestClient, administrator: dict[str, str]) -> None:
    image = built(dockyard, administrator)["id"]

    assert dockyard.delete(f"/api/images/{image}", headers=administrator).status_code == 204

    assert dockyard.get(f"/api/images/{image}", headers=administrator).status_code == 404
    assert dockyard.delete(f"/api/images/{image}", headers=administrator).status_code == 404


def test_an_image_deleted_while_it_is_built_is_gone_once_its_build_ends(
    dockyard: TestClient, administrator: dict[str, str], runtime: FakeRuntime
) -> None:
    with runtime.builds_paused():
        image = create_image(dockyard, administrator).json()["id"]
        assert dockyard.delete(f"/api/images/{image}", headers=administrator).status_code == 204

    assert not wait_until(  # never recorded again when its build ends
        lambda: dockyard.get(f"/api/images/{image}", headers=administrator).status_code != 404,
        timeout=0.5,
    )


def test_a_user_picks_a_ready_image_and_their_project_runs_in_it(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    image = built(dockyard, administrator)["id"]
    connect_claude_account(dockyard, user)

    created = create_project(dockyard, user, image=image)

    assert created.status_code == 202
    assert created.json()["image"] == image
    project = created.json()["id"]
    assert "rust-toolchain" in terminal_shows(dockyard, user, project, "cat /etc/rust")


def test_a_project_runs_in_the_base_image_unless_its_user_picks_another(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    built(dockyard, administrator)
    connect_claude_account(dockyard, user)

    created = create_project(dockyard, user)

    assert created.json()["image"] == "base"
    project = created.json()["id"]
    assert "No such file" in terminal_shows(dockyard, user, project, "cat /etc/rust")


@pytest.mark.parametrize("state", ["building", "failed", "unknown", "deleted"])
def test_a_project_is_refused_an_image_that_is_not_ready(
    dockyard: TestClient,
    administrator: dict[str, str],
    user: dict[str, str],
    runtime: FakeRuntime,
    state: str,
) -> None:
    image = "no-such-image"
    if state == "failed":
        image = built(dockyard, administrator, instructions="RUN false")["id"]
    elif state == "deleted":
        image = built(dockyard, administrator)["id"]
        dockyard.delete(f"/api/images/{image}", headers=administrator)
    with runtime.builds_paused():
        if state == "building":
            image = create_image(dockyard, administrator).json()["id"]

        refused = create_project(dockyard, user, image=image)

    assert refused.status_code == 422
    assert refused.json()["error"] == "image_not_available"
    assert refused.json()["message"]
    assert dockyard.get("/api/projects", headers=user).json() == []


def test_an_image_used_by_projects_cannot_be_deleted_and_the_refusal_names_them(
    dockyard: TestClient, administrator: dict[str, str], user: dict[str, str]
) -> None:
    image = built(dockyard, administrator)["id"]
    create_project(dockyard, user, image=image, name="Rusty")
    create_project(dockyard, administrator, image=image, name="Mine")

    refused = dockyard.delete(f"/api/images/{image}", headers=administrator)

    assert refused.status_code == 409
    assert refused.json()["error"] == "in_use"
    assert f"Rusty ({USER})" in refused.json()["message"]
    assert f"Mine ({ADMINISTRATOR})" in refused.json()["message"]
    assert shown(dockyard, administrator, image)["status"] == "ready"


def test_no_image_is_created_while_docker_is_unreachable(
    dockyard: TestClient, administrator: dict[str, str], runtime: FakeRuntime
) -> None:
    with runtime.unavailable():
        refused = create_image(dockyard, administrator)

    assert refused.status_code == 503
    assert refused.json()["error"] == "runtime_unavailable"
    assert len(dockyard.get("/api/images", headers=administrator).json()) == 1  # the Base Image


def test_instructions_may_hold_lines_that_start_with_from_in_lowercase(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    instructions = "RUN python3 - <<EOF\nfrom pathlib import Path\nEOF"

    assert create_image(dockyard, administrator, instructions=instructions).status_code == 202
