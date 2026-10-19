"""What only Docker can show about a Project's environment: how it survives crashes, the network
it is on, what it mounts, and where its Git Credential is not. These look at Docker directly, from
outside the environment."""

import ipaddress
import json
from collections.abc import Callable

import docker
import pytest
from docker.models.containers import Container

from dockyard.runtime import GitToken, ProjectStatus, Runtime

from .conftest import PUBLIC_REPOSITORY, new_project, wait_until
from .test_clone import finished
from .test_project_environment import cloned, server_ready

TOKEN = "github_pat_11DOCKYARD0never0on0disk"

pytestmark = [
    pytest.mark.slow,
    pytest.mark.parametrize("make_runtime", ["docker"], indirect=True),
]


def environment_of(daemon: docker.DockerClient, project: str, kind: str = "project") -> Container:
    (found,) = daemon.containers.list(
        all=True, filters={"label": [f"dockyard.project={project}", f"dockyard.kind={kind}"]}
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


def test_the_git_credential_is_neither_in_the_metadata_nor_in_variables_nor_on_disk(
    runtime: Runtime, new_user: Callable[[], str], docker_daemon: docker.DockerClient
) -> None:
    user = new_user()
    project = new_project()
    runtime.start_clone(user, project, PUBLIC_REPOSITORY, credential=GitToken("github.com", TOKEN))
    finished(runtime, project)
    clone = environment_of(docker_daemon, project, kind="clone")
    assert TOKEN not in json.dumps(clone.attrs)
    runtime.finish_clone(project)
    runtime.start_project(user, project, name="Hello", permission_mode="default")
    server_ready(runtime, project)

    runtime.write_git_credential(project, GitToken("github.com", TOKEN))

    environment = environment_of(docker_daemon, project)
    assert TOKEN not in json.dumps(environment.attrs)
    assert environment.attrs["HostConfig"]["Tmpfs"] == {
        "/run/dockyard": "uid=1000,gid=1000,mode=0700,size=1m"
    }
    # Everywhere the non-root user can write, and so leave it: only the tmpfs holds it.
    places = "/run/dockyard /workspace /home/ubuntu /tmp /var/tmp /dev/shm"
    found = run(environment, f"grep -rlsF -- {TOKEN} {places}; true")
    assert found.split() == ["/run/dockyard/gh/hosts.yml"]
    assert TOKEN not in run(environment, "cat /proc/*/environ")


def run(environment: Container, script: str) -> str:
    """What `script` prints, run by the non-root user in the environment."""
    exit_code, output = environment.exec_run(["bash", "-c", script], user="1000:1000")
    assert exit_code == 0 and isinstance(output, bytes), output
    return output.decode(errors="replace")


def test_a_projects_address_and_its_gateway_are_in_dockers_networks(
    runtime: Runtime, new_user: Callable[[], str], docker_daemon: docker.DockerClient
) -> None:
    """The local listener refuses them: a Project must never pass as a device on the LAN."""
    user = new_user()
    project = cloned(runtime, user)
    runtime.start_project(user, project, name="Hello", permission_mode="default")

    (attached,) = (
        environment_of(docker_daemon, project).attrs["NetworkSettings"]["Networks"].values()
    )
    networks = runtime.networks()

    for address in (attached["IPAddress"], attached["Gateway"]):
        assert any(ipaddress.ip_address(address) in network for network in networks), address
