"""Seam B harness: the Runtime contract, held against the in-memory fake and against Docker.

The Docker variant reaches Docker through a socket proxy configured like the one in compose.yaml
and runs the Base Image built from images/base, with a stand-in for Claude Code
(tests/runtime/stand-in). It needs a Docker daemon and is slow: the first run builds the Base Image.
"""

from __future__ import annotations

import secrets
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any

import docker
import pytest
import yaml
from docker.errors import APIError, DockerException, NotFound

from dockyard.runtime import Output, Runtime, Tty
from dockyard.runtime.docker import DockerRuntime
from dockyard.runtime.fake import FakeRuntime

HERE = Path(__file__).parent
REPO = HERE.parents[1]
BASE_IMAGE = "dockyard-base:test"
CONTRACT_IMAGE = "dockyard-base:contract"
TEST_LABEL = "dockyard.contract-test"
# Real repositories on github.com: the Docker variant clones from the internet.
PUBLIC_REPOSITORY = "https://github.com/octocat/Hello-World"
MISSING_REPOSITORY = "https://github.com/octocat/dockyard-no-such-repository"

MakeRuntime = Callable[..., Runtime]


@pytest.fixture(scope="session")
def docker_daemon() -> docker.DockerClient:
    """Direct access to Docker, for the harness only: the Runtime goes through the socket proxy."""
    try:
        client = docker.from_env()
        client.ping()
    except DockerException as error:
        pytest.skip(
            f"Docker is unreachable, so the Runtime contract runs on the fake only: {error}"
        )
    return client


def _build(tag: str, context: Path, **build_args: str) -> None:
    arguments = [
        arg for name, value in build_args.items() for arg in ("--build-arg", f"{name}={value}")
    ]
    subprocess.run(
        ["docker", "build", "--quiet", "--tag", tag, *arguments, str(context)],
        check=True,
        stdout=subprocess.DEVNULL,
    )


@pytest.fixture(scope="session")
def base_image(docker_daemon: docker.DockerClient) -> str:
    """The Base Image, built from images/base as it is now."""
    _build(BASE_IMAGE, REPO / "images" / "base")
    return BASE_IMAGE


@pytest.fixture(scope="session")
def contract_image(base_image: str) -> str:
    _build(CONTRACT_IMAGE, HERE / "stand-in", BASE_IMAGE=base_image)
    return CONTRACT_IMAGE


@pytest.fixture(scope="session")
def socket_proxy(docker_daemon: docker.DockerClient) -> Iterator[str]:
    """The socket proxy of compose.yaml, with the same image and settings."""
    compose = yaml.safe_load((REPO / "compose.yaml").read_text())
    service: dict[str, Any] = compose["services"]["socket-proxy"]
    settings: dict[str, Any] = {
        "environment": {name: str(value) for name, value in service["environment"].items()},
        "volumes": service["volumes"],
        "ports": {"2375/tcp": ("127.0.0.1", None)},  # any free port
    }
    proxy = docker_daemon.containers.run(
        service["image"], labels={TEST_LABEL: "1"}, detach=True, **settings
    )
    try:
        url = _wait_until_serving(proxy)
        yield url
    finally:
        proxy.remove(force=True)


def _wait_until_serving(proxy: Any) -> str:
    deadline = time.monotonic() + 30
    while True:
        proxy.reload()
        bindings = proxy.ports.get("2375/tcp")
        if bindings:
            url = f"tcp://127.0.0.1:{bindings[0]['HostPort']}"
            try:
                docker.DockerClient(base_url=url).ping()
                return url
            except DockerException:
                pass
        if time.monotonic() > deadline:
            raise RuntimeError(f"The socket proxy did not start: {proxy.logs().decode()}")
        time.sleep(0.2)


@pytest.fixture(params=["fake", pytest.param("docker", marks=pytest.mark.slow)])
def make_runtime(request: pytest.FixtureRequest) -> MakeRuntime:
    """Builds the Runtime under test, with optional settings (e.g. login_timeout)."""
    if request.param == "fake":

        def make_fake(**settings: Any) -> Runtime:
            fake = FakeRuntime(**settings)
            fake.publish_repository(PUBLIC_REPOSITORY)  # as it is on github.com
            return fake

        return make_fake
    image = request.getfixturevalue("contract_image")
    proxy = request.getfixturevalue("socket_proxy")

    def make(**settings: Any) -> Runtime:
        return DockerRuntime(docker.DockerClient(base_url=proxy), base_image=image, **settings)

    return make


@pytest.fixture
def runtime(make_runtime: MakeRuntime) -> Runtime:
    return make_runtime()


@pytest.fixture
def new_user(request: pytest.FixtureRequest) -> Iterator[Callable[[], str]]:
    """Each call is a new User, by the id that names their resources in the Runtime."""
    users: list[str] = []

    def new_user() -> str:
        users.append(f"contract{secrets.token_hex(4)}")
        return users[-1]

    yield new_user
    if request.node.callspec.params.get("make_runtime") == "docker":
        daemon: docker.DockerClient = request.getfixturevalue("docker_daemon")
        for user in users:
            label: dict[str, str | list[str] | bool] = {"label": f"dockyard.user={user}"}
            for container in daemon.containers.list(all=True, filters=label):
                with suppress(NotFound, APIError):  # gone, or being removed by Docker already
                    container.remove(force=True)
            for volume in daemon.volumes.list(filters=label):
                _remove_when_unused(volume)


@pytest.fixture
def new_image(request: pytest.FixtureRequest) -> Iterator[Callable[[], str]]:
    """Each call is a new Image, by its id, removed from Docker after the test."""
    images: list[str] = []

    def new_image() -> str:
        images.append(secrets.token_hex(8))
        return images[-1]

    yield new_image
    if request.node.callspec.params.get("make_runtime") == "docker":
        daemon: docker.DockerClient = request.getfixturevalue("docker_daemon")
        for image in images:
            for found in daemon.images.list(filters={"label": f"dockyard.image={image}"}):
                with suppress(NotFound, APIError):
                    daemon.images.remove(str(found.id), force=True)


def new_project() -> str:
    """A new Project, by its id. Its resources carry its User's label, so new_user cleans up."""
    return secrets.token_hex(8)


def _remove_when_unused(volume: Any) -> None:
    """Containers being auto-removed may still hold the volume for a moment."""
    deadline = time.monotonic() + 10
    while True:
        try:
            volume.remove(force=True)
            return
        except NotFound:
            return
        except APIError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)


class Screen[T: Output]:
    """Watches a Tty (or any output) from a background thread and keeps everything it printed."""

    def __init__(self, tty: T) -> None:
        self.tty = tty
        self._printed = b""
        self._ended = False
        self._changed = threading.Condition()
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self) -> None:
        while data := self.tty.read():
            with self._changed:
                self._printed += data
                self._changed.notify_all()
        with self._changed:
            self._ended = True
            self._changed.notify_all()

    @property
    def text(self) -> str:
        return self._printed.decode(errors="replace")

    def type(self: Screen[Tty], keys: str) -> None:
        self.tty.write(keys.encode())

    def wait_for(self, text: str, timeout: float = 20) -> None:
        with self._changed:
            if not self._changed.wait_for(lambda: text in self.text, timeout):
                raise AssertionError(f"{text!r} never appeared. The TTY shows: {self.text!r}")

    def wait_until_ended(self, timeout: float = 20) -> None:
        with self._changed:
            if not self._changed.wait_for(lambda: self._ended, timeout):
                raise AssertionError(f"The TTY did not end. It shows: {self.text!r}")


def wait_until(condition: Callable[[], bool], timeout: float = 20) -> bool:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True


LoseTrust = Callable[[Runtime, str], None]


@pytest.fixture
def lose_trust(request: pytest.FixtureRequest) -> LoseTrust:
    """Another writer of the User's Claude config drops the trust of the Project's path (the lost
    update the spec warns about), then its Remote Control Server restarts, as after a crash."""
    if request.node.callspec.params["make_runtime"] == "fake":

        def lose_in_fake(runtime: Runtime, project: str) -> None:
            assert isinstance(runtime, FakeRuntime)
            runtime.lose_trust(project)

        return lose_in_fake
    daemon: docker.DockerClient = request.getfixturevalue("docker_daemon")

    def lose(runtime: Runtime, project: str) -> None:
        labels = [f"dockyard.project={project}", "dockyard.kind=project"]
        (environment,) = daemon.containers.list(filters={"label": labels})
        forget = (
            'config="$CLAUDE_CONFIG_DIR/.claude.json"; '
            'jq --arg p "$PWD" "del(.projects[\\$p])" "$config" > "$config.new" && '
            'chmod 600 "$config.new" && mv "$config.new" "$config"'
        )
        for command in (["bash", "-c", forget], ["pkill", "-f", "sleep infinity"]):
            exit_code, output = environment.exec_run(command, workdir=f"/workspace/{project}")
            assert exit_code == 0, output

    return lose


@pytest.fixture
def exit_by_itself(request: pytest.FixtureRequest) -> Callable[[Runtime, str], None]:
    """The Project's Remote Control Server exits by itself, as in a crash: Docker starts its
    environment again."""
    if request.node.callspec.params["make_runtime"] == "fake":

        def exit_in_fake(runtime: Runtime, project: str) -> None:
            assert isinstance(runtime, FakeRuntime)
            runtime.exit_server(project)

        return exit_in_fake
    daemon: docker.DockerClient = request.getfixturevalue("docker_daemon")

    def exit_server(runtime: Runtime, project: str) -> None:
        labels = [f"dockyard.project={project}", "dockyard.kind=project"]
        (environment,) = daemon.containers.list(filters={"label": labels})
        exit_code, output = environment.exec_run(["pkill", "-f", "sleep infinity"])
        assert exit_code == 0, output

    return exit_server


TerminalProcesses = Callable[[Runtime, str], int]


@pytest.fixture
def terminal_processes(request: pytest.FixtureRequest) -> TerminalProcesses:
    """How many processes the Project's Terminals run: their shells, and the `sleep 600` typed in
    them."""
    if request.node.callspec.params["make_runtime"] == "fake":

        def count_in_fake(runtime: Runtime, project: str) -> int:
            assert isinstance(runtime, FakeRuntime)
            return runtime.terminal_processes(project)

        return count_in_fake
    daemon: docker.DockerClient = request.getfixturevalue("docker_daemon")

    def count(runtime: Runtime, project: str) -> int:
        labels = [f"dockyard.project={project}", "dockyard.kind=project"]
        (environment,) = daemon.containers.list(filters={"label": labels})
        processes = 0
        for pattern in (["-x", "bash"], ["-f", "-x", "sleep 600"]):
            result = environment.exec_run(["pgrep", "-c", *pattern])
            assert isinstance(result.output, bytes)
            processes += int(result.output)
        return processes

    return count
