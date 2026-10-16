"""Projects respect the Instance limits: the CPU and RAM of each Project stay within the
per-Project maximums, and a User has no more active Projects than the per-User maximum."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, AccessProxy, connect_claude_account, wait_until

REPOSITORY = "https://github.com/octocat/Hello-World"
GIB = 1024**3
OTHER_USER = "another.user@example.com"


@pytest.fixture(autouse=True)
def public_repository(runtime: FakeRuntime) -> None:
    runtime.publish_repository(REPOSITORY)


@pytest.fixture
def signed_in(proxy: AccessProxy) -> dict[str, str]:
    return proxy.headers(ADMINISTRATOR)


@pytest.fixture
def limited(start_dockyard: Callable[..., TestClient]) -> TestClient:
    """An Instance whose Projects may use up to 3 CPUs and 6 GiB, two active per User."""
    return start_dockyard(
        DOCKYARD_PROJECT_MAX_CPUS="3",
        DOCKYARD_PROJECT_MAX_MEMORY_GIB="6",
        DOCKYARD_MAX_ACTIVE_PROJECTS="2",
    )


def create(dockyard: TestClient, headers: dict[str, str], **fields: Any) -> Any:
    body = {"name": "Hello", "repository": REPOSITORY, "visibility": "public", **fields}
    return dockyard.post("/api/projects", json=body, headers=headers)


@pytest.mark.parametrize(
    ("fields", "error", "message"),
    [
        ({"cpus": 3.5}, "cpus_out_of_range", "between 0.5 and 3 CPUs"),
        ({"cpus": 0.25}, "cpus_out_of_range", "between 0.5 and 3 CPUs"),
        ({"memory": 7 * GIB}, "memory_out_of_range", "between 1 and 6 GiB"),
        ({"memory": GIB // 2}, "memory_out_of_range", "between 1 and 6 GiB"),
    ],
)
def test_a_project_beyond_the_cpu_or_ram_limits_is_refused_with_the_allowed_range(
    limited: TestClient, signed_in: dict[str, str], fields: dict[str, Any], error: str, message: str
) -> None:
    refused = create(limited, signed_in, **fields)

    assert refused.status_code == 422
    assert refused.json()["error"] == error
    assert message in refused.json()["message"]
    assert limited.get("/api/projects", headers=signed_in).json() == []


def test_a_project_runs_with_the_cpu_and_ram_its_user_chose(
    limited: TestClient, signed_in: dict[str, str]
) -> None:
    connect_claude_account(limited, signed_in)

    project = create(limited, signed_in, cpus=1.5, memory=2 * GIB).json()

    assert (project["cpus"], project["memory"]) == (1.5, 2 * GIB)
    stats = running_stats(limited, signed_in, project["id"])
    assert (stats["cpus"], stats["memory_limit"]) == (1.5, 2 * GIB)


def test_a_project_gets_the_instance_maximums_unless_its_user_chooses_less(
    limited: TestClient, signed_in: dict[str, str]
) -> None:
    connect_claude_account(limited, signed_in)

    project = create(limited, signed_in).json()

    assert (project["cpus"], project["memory"]) == (3, 6 * GIB)
    stats = running_stats(limited, signed_in, project["id"])
    assert (stats["cpus"], stats["memory_limit"]) == (3, 6 * GIB)


def test_a_fresh_instance_gives_each_project_two_cpus_and_four_gib(
    dockyard: TestClient, signed_in: dict[str, str]
) -> None:
    project = create(dockyard, signed_in).json()

    assert (project["cpus"], project["memory"]) == (2, 4 * GIB)
    assert create(dockyard, signed_in, cpus=2.5).json()["error"] == "cpus_out_of_range"


def test_a_project_created_under_higher_maximums_starts_within_the_current_ones(
    start_dockyard: Callable[..., TestClient], signed_in: dict[str, str]
) -> None:
    before = start_dockyard(DOCKYARD_PROJECT_MAX_CPUS="4", DOCKYARD_PROJECT_MAX_MEMORY_GIB="8")
    project = create(before, signed_in).json()["id"]
    assert becomes(before, signed_in, project, "waiting")

    lowered = start_dockyard(DOCKYARD_PROJECT_MAX_CPUS="1", DOCKYARD_PROJECT_MAX_MEMORY_GIB="2")
    connect_claude_account(lowered, signed_in)

    stats = running_stats(lowered, signed_in, project)
    assert (stats["cpus"], stats["memory_limit"]) == (1, 2 * GIB)


def test_creating_a_project_beyond_the_active_maximum_is_refused(
    limited: TestClient, signed_in: dict[str, str], runtime: FakeRuntime
) -> None:
    with runtime.clones_paused():  # being cloned counts as active
        assert create(limited, signed_in, name="First").status_code == 202
        assert create(limited, signed_in, name="Second").status_code == 202

        refused = create(limited, signed_in, name="Third")

    assert refused.status_code == 409
    assert refused.json()["error"] == "active_maximum"
    message = refused.json()["message"]
    assert "maximum of Active Projects this Instance allows each User (2)" in message
    names = [p["name"] for p in limited.get("/api/projects", headers=signed_in).json()]
    assert names == ["First", "Second"]


def test_a_stopped_project_frees_its_place_among_the_active_ones(
    limited: TestClient, signed_in: dict[str, str]
) -> None:
    first = create(limited, signed_in, name="First").json()["id"]
    create(limited, signed_in, name="Second")
    assert becomes(limited, signed_in, first, "waiting")  # waiting counts as active
    assert create(limited, signed_in, name="Third").status_code == 409

    act(limited, signed_in, first, "stop")

    assert create(limited, signed_in, name="Third").status_code == 202


def test_a_project_stopped_while_being_cloned_stays_stopped_and_frees_its_place(
    limited: TestClient, signed_in: dict[str, str], runtime: FakeRuntime
) -> None:
    connect_claude_account(limited, signed_in)
    with runtime.clones_paused():
        first = create(limited, signed_in, name="First").json()["id"]
        create(limited, signed_in, name="Second")

        act(limited, signed_in, first, "stop")

    assert becomes(limited, signed_in, first, "stopped")
    assert create(limited, signed_in, name="Third").status_code == 202
    assert status_of(limited, signed_in, first) == "stopped"


def test_starting_a_project_beyond_the_active_maximum_is_refused(
    limited: TestClient, signed_in: dict[str, str]
) -> None:
    connect_claude_account(limited, signed_in)
    first = create(limited, signed_in, name="First").json()["id"]
    assert becomes(limited, signed_in, first, "running")
    act(limited, signed_in, first, "stop")
    second = create(limited, signed_in, name="Second").json()["id"]
    third = create(limited, signed_in, name="Third").json()["id"]
    assert becomes(limited, signed_in, second, "running")
    assert becomes(limited, signed_in, third, "running")

    refused = act(limited, signed_in, first, "start")

    assert refused.status_code == 409
    assert refused.json()["error"] == "active_maximum"
    assert status_of(limited, signed_in, first) == "stopped"
    assert act(limited, signed_in, second, "restart").status_code == 200  # already active
    act(limited, signed_in, second, "stop")
    assert act(limited, signed_in, first, "start").json()["status"] == "running"


def test_the_active_maximum_counts_each_users_projects_on_their_own(
    limited: TestClient,
    signed_in: dict[str, str],
    proxy: AccessProxy,
    add_user: Callable[[str], None],
) -> None:
    create(limited, signed_in, name="First")
    create(limited, signed_in, name="Second")
    add_user(OTHER_USER)

    assert create(limited, proxy.headers(OTHER_USER), name="Theirs").status_code == 202


def running_stats(dockyard: TestClient, headers: dict[str, str], project: str) -> Any:
    assert becomes(dockyard, headers, project, "running")
    stats = dockyard.get(f"/api/projects/{project}/stats", headers=headers)
    assert stats.status_code == 200, stats.text
    return stats.json()


def status_of(dockyard: TestClient, headers: dict[str, str], project: str) -> str:
    status: str = dockyard.get(f"/api/projects/{project}", headers=headers).json()["status"]
    return status


def becomes(dockyard: TestClient, headers: dict[str, str], project: str, status: str) -> bool:
    return wait_until(lambda: status_of(dockyard, headers, project) == status)


def act(dockyard: TestClient, headers: dict[str, str], project: str, action: str) -> Any:
    return dockyard.post(f"/api/projects/{project}/{action}", headers=headers)
