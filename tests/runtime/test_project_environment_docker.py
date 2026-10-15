"""What only Docker can show about a Project's environment: how it survives crashes, the network
it is on, and what it mounts. These look at Docker directly, from outside the environment."""

from collections.abc import Callable

import docker
import pytest
from docker.models.containers import Container

from dockyard.runtime import ProjectStatus, Runtime

from .conftest import wait_until
from .test_project_environment import cloned, server_ready

pytestmark = [
    pytest.mark.slow,
    pytest.mark.parametrize("make_runtime", ["docker"], indirect=True),
]


def environment_of(daemon: docker.DockerClient, project: str) -> Container:
    (found,) = daemon.containers.list(
        all=True, filters={"label": [f"dockyard.project={project}", "dockyard.kind=project"]}
    )
    return found


def test_a_crashed_server_is_restarted(
    runtime: Runtime, new_user: Callable[[], str], docker_daemon: docker.DockerClient
) -> None:
    user = new_user()
    project = cloned(runtime, user)
    runtime.start_project(user, project, name="Hello", permission_mode="default")
    server_ready(runtime, project)

    environment = environment_of(docker_daemon, project)
    environment.exec_run(["pkill", "-f", "sleep infinity"])

    assert wait_until(lambda: "starts: 2" in runtime.project_log(project), timeout=30)
    assert runtime.project_status(project) == ProjectStatus.RUNNING


def test_projects_are_alone_on_a_network_without_inter_container_traffic(
    runtime: Runtime, new_user: Callable[[], str], docker_daemon: docker.DockerClient
) -> None:
    user = new_user()
    project = cloned(runtime, user)
    runtime.start_project(user, project, name="Hello", permission_mode="default")

    environment = environment_of(docker_daemon, project)
    (network,) = environment.attrs["NetworkSettings"]["Networks"]
    details = docker_daemon.networks.get(network).attrs
    assert details["Driver"] == "bridge"
    assert details["Options"]["com.docker.network.bridge.enable_icc"] == "false"
    assert details["Options"]["com.docker.network.bridge.name"] == "dockyard0"
    members = [docker_daemon.containers.get(member) for member in details["Containers"]]
    assert {member.labels.get("dockyard.kind") for member in members} == {"project"}


def test_a_project_mounts_only_its_own_volumes_and_the_users_claude_config(
    runtime: Runtime, new_user: Callable[[], str], docker_daemon: docker.DockerClient
) -> None:
    user = new_user()
    project = cloned(runtime, user)
    runtime.start_project(user, project, name="Hello", permission_mode="default")

    mounts = environment_of(docker_daemon, project).attrs["Mounts"]
    assert {(m["Type"], m["Name"], m["Destination"]) for m in mounts} == {
        ("volume", f"dockyard-code-{project}", f"/workspace/{project}"),
        ("volume", f"dockyard-home-{project}", "/home/ubuntu"),
        ("volume", f"dockyard-claude-config-{user}", "/home/ubuntu/.claude"),
    }


def test_the_projects_network_exists_once_dockyard_has_started(
    runtime: Runtime, docker_daemon: docker.DockerClient
) -> None:
    runtime.ensure_infrastructure()
    runtime.ensure_infrastructure()  # again: nothing changes

    (network,) = docker_daemon.networks.list(names=["dockyard-projects"])
    assert network.attrs["Options"]["com.docker.network.bridge.enable_icc"] == "false"
