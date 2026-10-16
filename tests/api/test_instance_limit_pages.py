"""The new Project form within the Instance limits: the ranges it offers, and its refusals."""

import re
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, AccessProxy, connect_claude_account, wait_until

REPOSITORY = "https://github.com/octocat/Hello-World"


@pytest.fixture(autouse=True)
def public_repository(runtime: FakeRuntime) -> None:
    runtime.publish_repository(REPOSITORY)


@pytest.fixture
def signed_in(proxy: AccessProxy) -> dict[str, str]:
    return proxy.headers(ADMINISTRATOR)


@pytest.fixture
def limited(start_dockyard: Callable[..., TestClient]) -> TestClient:
    """An Instance whose Projects may use up to 3 CPUs and 6 GiB, one active per User."""
    return start_dockyard(
        DOCKYARD_PROJECT_MAX_CPUS="3",
        DOCKYARD_PROJECT_MAX_MEMORY_GIB="6",
        DOCKYARD_MAX_ACTIVE_PROJECTS="1",
    )


def submit(dockyard: TestClient, headers: dict[str, str], name: str, **fields: str) -> Response:
    return dockyard.post(
        "/projects",
        data={"name": name, "repository": REPOSITORY, "visibility": "public", **fields},
        headers=headers,
    )


def field(page: str, name: str) -> str:
    """The form's input named `name`."""
    found = re.search(rf'<input [^>]*name="{name}"[^>]*>', page)
    assert found, f"No {name} field on the page"
    return found.group()


def test_the_new_project_form_offers_the_allowed_ranges_with_the_maximums_by_default(
    limited: TestClient, signed_in: dict[str, str]
) -> None:
    form = limited.get("/projects/new", headers=signed_in).text

    cpus, memory = field(form, "cpus"), field(form, "memory")
    for attribute in ('min="0.5"', 'max="3"', 'value="3"'):
        assert attribute in cpus
    for attribute in ('min="1"', 'max="6"', 'value="6"'):
        assert attribute in memory
    assert "Between 0.5 and 3; 3 unless you choose less." in form
    assert "Between 1 and 6 GiB; 6 unless you choose less." in form
    assert "Active Projects you may have at once: 1." in form


def test_a_project_created_from_the_form_shows_the_cpu_and_ram_chosen(
    limited: TestClient, signed_in: dict[str, str]
) -> None:
    page = submit(limited, signed_in, "Small", cpus="1.5", memory="2.5")

    assert page.status_code == 200
    assert page.url.path.startswith("/projects/")
    assert "CPUs up to 1.5, RAM up to 2.5 GiB" in page.text


def test_a_form_beyond_the_limits_is_refused_with_the_range_and_keeps_what_was_typed(
    limited: TestClient, signed_in: dict[str, str]
) -> None:
    page = submit(limited, signed_in, "Huge", cpus="2", memory="8")

    assert page.status_code == 422
    assert "Choose between 1 and 6 GiB of RAM" in page.text
    assert 'value="Huge"' in page.text
    assert 'value="2"' in field(page.text, "cpus")
    assert 'value="8"' in field(page.text, "memory")
    assert "Huge" not in limited.get("/", headers=signed_in).text


def test_the_form_explains_when_the_active_maximum_is_reached(
    limited: TestClient, signed_in: dict[str, str]
) -> None:
    submit(limited, signed_in, "First")

    page = submit(limited, signed_in, "Second")

    assert page.status_code == 422
    assert "maximum of Active Projects this Instance allows each User (1)" in page.text
    assert 'value="Second"' in page.text


def test_starting_from_the_project_page_beyond_the_active_maximum_is_explained(
    limited: TestClient, signed_in: dict[str, str]
) -> None:
    connect_claude_account(limited, signed_in)
    first = submit(limited, signed_in, "First").url.path
    limited.post(f"{first}/stop", headers=signed_in)
    second = submit(limited, signed_in, "Second").url.path
    assert wait_until(lambda: "Restart" in limited.get(second, headers=signed_in).text)

    page = limited.post(f"{first}/start", headers=signed_in)

    assert page.status_code == 409
    assert "Too many Active Projects" in page.text
    assert "stop one of them first" in page.text
