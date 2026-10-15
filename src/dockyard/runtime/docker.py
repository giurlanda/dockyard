"""Docker adapter. It reaches Docker only through DOCKER_HOST, which points to the socket proxy."""

from __future__ import annotations

import os
import secrets
import socket
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

import docker
import requests
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.models.containers import Container

from . import (
    CLONE_TIMEOUT,
    LOGIN_TIMEOUT,
    Clone,
    CloneState,
    LoginNotRunning,
    ProjectStatus,
    RuntimeUnavailable,
    project_path,
)

UID = 1000  # "ubuntu", the Base Image's non-root user
CLAUDE_CONFIG_DIR = "/home/ubuntu/.claude"  # CLAUDE_CONFIG_DIR in the Base Image
CREDENTIALS = f"{CLAUDE_CONFIG_DIR}/.credentials.json"
# The Base Image's working directory. Never a volume mount point: with Docker 29 that resets the
# volume's root to root:root. Project paths are below it.
WORKSPACE = "/workspace"
HANDOVER_MOUNT = "/volume"  # where a new volume is mounted to hand it to the non-root user

KIND_LABEL = "dockyard.kind"
USER_LABEL = "dockyard.user"
PROJECT_LABEL = "dockyard.project"
LOGIN_ENVIRONMENT = "login-environment"
CLONE = "clone"

# Every environment that is not a Privileged Project runs with these.
HARDENING: dict[str, Any] = {
    "cap_drop": ["ALL"],
    "security_opt": ["no-new-privileges:true"],
    "pids_limit": 512,
}


class DockerRuntime:
    def __init__(
        self, client: docker.DockerClient, *, base_image: str, login_timeout: float = LOGIN_TIMEOUT
    ) -> None:
        self._client = client
        self._base_image = base_image
        self._login_timeout = login_timeout

    @classmethod
    def from_env(cls, *, base_image: str) -> DockerRuntime:
        try:
            return cls(docker.from_env(), base_image=base_image)
        except DockerException as error:
            raise _unavailable(error) from error

    def check_reachable(self) -> None:
        try:
            self._client.ping()
        except (DockerException, requests.RequestException) as error:
            raise _unavailable(error) from error

    def claude_account_connected(self, user: str) -> bool:
        with self._docker():
            try:
                self._client.volumes.get(_claude_config_volume(user))
            except NotFound:
                return False
            exit_status = self._run_job(
                ["test", "-s", CREDENTIALS],
                volumes={_claude_config_volume(user): {"bind": CLAUDE_CONFIG_DIR, "mode": "ro"}},
                labels=_labels("claude-config-check", user),
            )
            return exit_status == 0

    def start_login_environment(self, user: str) -> None:
        with self._docker():
            self.stop_login_environment(user)
            login = self._client.containers.create(
                self._base_image,
                ["timeout", "--foreground", f"{self._login_timeout:g}", "claude", "auth", "login"],
                name=f"dockyard-{LOGIN_ENVIRONMENT}-{user}-{secrets.token_hex(3)}",
                volumes={
                    self._ensure_claude_config(user): {"bind": CLAUDE_CONFIG_DIR, "mode": "rw"}
                },
                environment={"TERM": "xterm-256color"},
                labels=_labels(LOGIN_ENVIRONMENT, user),
                tty=True,
                stdin_open=True,
                init=True,
                auto_remove=True,
                mem_limit="1g",
                nano_cpus=1_000_000_000,
                **HARDENING,
            )
            login.start()

    def login_environment_running(self, user: str) -> bool:
        with self._docker():
            return self._login(user) is not None

    def attach_login_environment(self, user: str) -> AttachedTty:
        with self._docker():
            login = self._login(user)
            if login is None:
                raise LoginNotRunning
            try:
                return AttachedTty(self._client.api, str(login.id))
            except NotFound as error:  # it ended in the meantime
                raise LoginNotRunning from error

    def stop_login_environment(self, user: str) -> None:
        with self._docker():
            for login in self._login_environments(user):
                try:
                    login.remove(force=True)
                except NotFound:
                    pass
                except APIError as error:
                    if "already in progress" not in str(error):  # its program ended: auto-removed
                        raise

    def start_clone(self, user: str, project: str, repository: str) -> None:
        path = project_path(project)
        labels = {**_labels(CLONE, user), PROJECT_LABEL: project}
        with self._docker():
            code = self._client.volumes.create(
                _code_volume(project), labels={**_labels("code", user), PROJECT_LABEL: project}
            ).name
            self._hand_to_non_root_user(code, labels={**labels, KIND_LABEL: "code-chown"})
            clone = self._client.containers.create(
                self._base_image,
                [
                    "timeout",
                    f"{CLONE_TIMEOUT}",
                    "git",
                    "clone",
                    "--progress",
                    "--",
                    repository,
                    path,
                ],
                name=f"dockyard-{CLONE}-{project}",
                volumes={code: {"bind": path, "mode": "rw"}},
                working_dir=WORKSPACE,
                # A repository that is not public asks for a user name: fail instead of waiting.
                environment={"GIT_TERMINAL_PROMPT": "0"},
                labels=labels,
                init=True,
                mem_limit="1g",
                nano_cpus=1_000_000_000,
                **HARDENING,
            )
            clone.start()

    def clone(self, project: str) -> Clone | None:
        with self._docker():
            clones = self._project_containers(project, CLONE)
            if not clones:
                return None
            state = clones[0].attrs["State"]
            output = clones[0].logs().decode(errors="replace")
        if state["Status"] in ("created", "running", "restarting"):
            return Clone(CloneState.RUNNING, output)
        succeeded = state["ExitCode"] == 0
        return Clone(CloneState.SUCCEEDED if succeeded else CloneState.FAILED, output)

    def finish_clone(self, project: str) -> None:
        with self._docker():
            for clone in self._project_containers(project, CLONE):
                clone.remove(force=True)

    def project_status(self, project: str) -> ProjectStatus:
        with self._docker():
            if self._project_containers(project, CLONE):
                return ProjectStatus.CLONING
            try:
                self._client.volumes.get(_code_volume(project))
            except NotFound:
                return ProjectStatus.MISSING
            return ProjectStatus.STOPPED

    def remove_project(self, project: str, *, code: bool) -> None:
        with self._docker():
            for container in self._project_containers(project):
                with suppress(NotFound):
                    container.remove(force=True)
            if code:
                with suppress(NotFound):
                    self._client.volumes.get(_code_volume(project)).remove(force=True)

    def _project_containers(self, project: str, kind: str | None = None) -> list[Container]:
        labels = [f"{PROJECT_LABEL}={project}"] + ([f"{KIND_LABEL}={kind}"] if kind else [])
        return self._client.containers.list(all=True, filters={"label": labels})

    def _hand_to_non_root_user(self, volume: str, labels: dict[str, str]) -> None:
        """A new volume belongs to root: hand it to the non-root user, with only CAP_CHOWN.

        Mounted where root reaches it without reading other directories: with only CAP_CHOWN,
        root cannot go through /home/ubuntu (mode 0750).
        """
        exit_status = self._run_job(
            ["chown", f"{UID}:{UID}", HANDOVER_MOUNT],
            volumes={volume: {"bind": HANDOVER_MOUNT, "mode": "rw"}},
            labels=labels,
            user="root",
            cap_add=["CHOWN"],
        )
        if exit_status != 0:
            raise RuntimeUnavailable(
                f"Could not hand {volume} to the non-root user: chown exited with {exit_status}"
            )

    def _ensure_claude_config(self, user: str) -> str:
        """The User's own Claude config volume, created on first use."""
        name = _claude_config_volume(user)
        try:
            self._client.volumes.get(name)
        except NotFound:
            volume = self._client.volumes.create(name, labels=_labels("claude-config", user))
            try:
                self._hand_to_non_root_user(name, labels=_labels("claude-config-chown", user))
            except RuntimeUnavailable:
                volume.remove(force=True)  # never left to root: the next use tries again
                raise
        return name

    def _login(self, user: str) -> Container | None:
        running = self._login_environments(user, status="running")
        return running[0] if running else None

    def _login_environments(self, user: str, **filters: str) -> list[Container]:
        labels = [f"{KIND_LABEL}={LOGIN_ENVIRONMENT}", f"{USER_LABEL}={user}"]
        return self._client.containers.list(all=True, filters={"label": labels, **filters})

    def _run_job(self, command: list[str], **options: Any) -> int:
        """Run a short command in the Base Image, offline, and return its exit status."""
        job = self._client.containers.create(
            self._base_image, command, network_disabled=True, **{**HARDENING, **options}
        )
        try:
            job.start()
            return int(job.wait(timeout=60)["StatusCode"])
        finally:
            job.remove(force=True)

    @contextmanager
    def _docker(self) -> Iterator[None]:
        """Docker's failures, as the Runtime reports them."""
        try:
            yield
        except ImageNotFound as error:
            raise RuntimeUnavailable(
                f"The Base Image {self._base_image} is not on this Docker host. "
                "Build it with: docker compose build"
            ) from error
        except (DockerException, requests.RequestException) as error:
            raise RuntimeUnavailable(f"Docker failed: {error}") from error


class AttachedTty:
    """The TTY of a running container's main process, through Docker's attach."""

    def __init__(self, api: docker.APIClient, container: str) -> None:
        self._api = api
        self._container = container
        stream = api.attach_socket(
            container, params={"stdin": 1, "stdout": 1, "stderr": 1, "stream": 1, "logs": 1}
        )
        self._response = stream._response
        self._socket: socket.socket = stream._sock
        self._unread = _read_ahead(self._response, self._socket)
        self._socket.settimeout(None)  # docker-py leaves the client's request timeout on it

    def read(self) -> bytes:
        if self._unread:
            data, self._unread = self._unread, b""
            return data
        try:
            return self._socket.recv(65536)
        except OSError:
            return b""

    def write(self, data: bytes) -> None:
        with suppress(OSError):  # the program has ended: read() says so
            self._socket.sendall(data)

    def resize(self, rows: int, cols: int) -> None:
        with suppress(DockerException, requests.RequestException):  # it ended: read() says so
            self._api.resize(self._container, height=rows, width=cols)

    def close(self) -> None:
        with suppress(OSError):  # wakes up a read() waiting in another thread
            self._socket.shutdown(socket.SHUT_RDWR)
        self._socket.close()
        self._response.close()


def _read_ahead(response: Any, sock: socket.socket) -> bytes:
    """What Docker sent right behind its "101 UPGRADED" answer.

    The HTTP client parsing that answer reads ahead into its own buffer, out of reach of the
    socket: through the socket proxy, an attach that replays what the program already printed
    loses most of it unless it is taken from there.
    """
    reader = response.raw._fp.fp
    sock.setblocking(False)
    try:
        return reader.read1(-1) or b""
    except BlockingIOError:
        return b""


def _claude_config_volume(user: str) -> str:
    return f"dockyard-claude-config-{user}"


def _code_volume(project: str) -> str:
    return f"dockyard-code-{project}"


def _labels(kind: str, user: str) -> dict[str, str]:
    return {KIND_LABEL: kind, USER_LABEL: user}


def _unavailable(error: Exception) -> RuntimeUnavailable:
    host = os.environ.get("DOCKER_HOST", "the default Docker socket")
    return RuntimeUnavailable(
        f"Dockyard cannot start: Docker is unreachable at {host} ({error}). "
        "Dockyard talks to Docker only through the socket proxy: check that the socket proxy "
        "is running and that DOCKER_HOST points to it."
    )
