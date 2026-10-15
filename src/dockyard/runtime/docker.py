"""Docker adapter. It reaches Docker only through DOCKER_HOST, which points to the socket proxy."""

from __future__ import annotations

import os
import secrets
import socket
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from typing import Any

import docker
import requests
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.models.containers import Container

from . import (
    CLONE_TIMEOUT,
    DEFAULT_DNS,
    LOGIN_TIMEOUT,
    PROJECT_CPUS,
    PROJECT_MEMORY,
    PROJECT_PIDS_LIMIT,
    Clone,
    CloneState,
    LoginNotRunning,
    ProjectMissing,
    ProjectStatus,
    RuntimeUnavailable,
    project_path,
)

UID = 1000  # "ubuntu", the Base Image's non-root user
HOME = "/home/ubuntu"
CLAUDE_CONFIG_DIR = "/home/ubuntu/.claude"  # CLAUDE_CONFIG_DIR in the Base Image
CREDENTIALS = f"{CLAUDE_CONFIG_DIR}/.credentials.json"
# The Base Image's working directory. Never a volume mount point: with Docker 29 that resets the
# volume's root to root:root. Project paths are below it.
WORKSPACE = "/workspace"
HANDOVER_MOUNT = "/volume"  # where a new volume is mounted to hand it to the non-root user
PROJECT_LOG_LINES = 1000
# Trusts the path $1 in the Claude config: see DockerRuntime._trust.
TRUST_SCRIPT = """
set -eu
config="$CLAUDE_CONFIG_DIR/.claude.json"
current=$(cat "$config" 2>/dev/null || true)
[ -n "$current" ] || current='{}'
trusted='.projects[$p].hasTrustDialogAccepted == true'
if printf '%s' "$current" | jq -e --arg p "$1" "$trusted" >/dev/null; then exit 0; fi
umask 077
printf '%s' "$current" | jq --arg p "$1" '.projects[$p].hasTrustDialogAccepted = true' \
    > "$config.dockyard-new"
chmod 600 "$config.dockyard-new"
mv -f "$config.dockyard-new" "$config"
"""

KIND_LABEL = "dockyard.kind"
USER_LABEL = "dockyard.user"
PROJECT_LABEL = "dockyard.project"
LOGIN_ENVIRONMENT = "login-environment"
CLONE = "clone"
PROJECT_ENVIRONMENT = "project"
# Every Project environment is on this network, and nothing else: inter-container traffic is off,
# and the host firewall knows its bridge by its interface name.
PROJECTS_NETWORK = "dockyard-projects"
PROJECTS_BRIDGE = "dockyard0"

# Every environment that is not a Privileged Project runs with these.
HARDENING: dict[str, Any] = {
    "cap_drop": ["ALL"],
    "security_opt": ["no-new-privileges:true"],
    "pids_limit": 512,
}


class DockerRuntime:
    def __init__(
        self,
        client: docker.DockerClient,
        *,
        base_image: str,
        dns: Sequence[str] = DEFAULT_DNS,
        login_timeout: float = LOGIN_TIMEOUT,
    ) -> None:
        self._client = client
        self._base_image = base_image
        self._dns = tuple(dns)
        self._login_timeout = login_timeout

    @classmethod
    def from_env(cls, *, base_image: str, dns: Sequence[str]) -> DockerRuntime:
        try:
            return cls(docker.from_env(), base_image=base_image, dns=dns)
        except DockerException as error:
            raise _unavailable(error) from error

    def check_reachable(self) -> None:
        try:
            self._client.ping()
        except (DockerException, requests.RequestException) as error:
            raise _unavailable(error) from error

    def ensure_infrastructure(self) -> None:
        with self._docker():
            self._ensure_network()

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

    def start_project(self, user: str, project: str, *, name: str, permission_mode: str) -> None:
        path = project_path(project)
        with self._docker():
            try:
                code = self._client.volumes.get(_code_volume(project)).name
            except NotFound as error:
                raise ProjectMissing(project) from error
            network = self._ensure_network()
            home = self._ensure_home(user, project)
            claude_config = self._ensure_claude_config(user)
            self._trust(user, claude_config, path)
            self.stop_project(project)
            environment = self._client.containers.create(
                self._base_image,
                # Never the working directory: that would reset the volume's root to root:root.
                [
                    *["bash", "-c", 'cd -- "$1" && shift && exec claude "$@"', "dockyard", path],
                    *["remote-control", f"--name={name}", "--spawn", "worktree"],
                    *["--permission-mode", permission_mode],
                ],
                name=f"dockyard-{PROJECT_ENVIRONMENT}-{project}",
                user=f"{UID}:{UID}",
                volumes={
                    code: {"bind": path, "mode": "rw"},
                    home: {"bind": HOME, "mode": "rw"},
                    claude_config: {"bind": CLAUDE_CONFIG_DIR, "mode": "rw"},
                },
                working_dir=WORKSPACE,
                environment={"TERM": "xterm-256color"},
                labels={**_labels(PROJECT_ENVIRONMENT, user), PROJECT_LABEL: project},
                # The console answers the Remote Control Server's prompts: a TTY, stdin kept open.
                tty=True,
                stdin_open=True,
                init=True,
                # Restarted after a crash and when Docker restarts, unless stopped by Dockyard.
                # The stubs only know "always" and "on-failure".
                restart_policy={"Name": "unless-stopped"},  # type: ignore[arg-type]
                network=network,
                dns=list(self._dns),
                mem_limit=PROJECT_MEMORY,
                nano_cpus=int(PROJECT_CPUS * 1_000_000_000),
                **{**HARDENING, "pids_limit": PROJECT_PIDS_LIMIT},
            )
            environment.start()

    def stop_project(self, project: str) -> None:
        with self._docker():
            for environment in self._project_containers(project, PROJECT_ENVIRONMENT):
                with suppress(NotFound):
                    environment.stop(timeout=10)  # the server can keep its Sessions resumable
                    environment.remove(force=True)

    def project_log(self, project: str) -> str:
        with self._docker():
            environments = self._project_containers(project, PROJECT_ENVIRONMENT)
            if not environments:
                return ""
            return environments[0].logs(tail=PROJECT_LOG_LINES).decode(errors="replace")

    def project_status(self, project: str) -> ProjectStatus:
        with self._docker():
            if self._project_containers(project, CLONE):
                return ProjectStatus.CLONING
            try:
                self._client.volumes.get(_code_volume(project))
            except NotFound:
                return ProjectStatus.MISSING
            environments = self._project_containers(project, PROJECT_ENVIRONMENT)
            if not environments:
                return ProjectStatus.STOPPED
            if environments[0].status == "running":
                return ProjectStatus.RUNNING
            return ProjectStatus.FAILED

    def remove_project(self, project: str, *, code: bool) -> None:
        with self._docker():
            for container in self._project_containers(project):
                with suppress(NotFound):
                    container.remove(force=True)
            if code:
                for volume in (_code_volume(project), _home_volume(project)):
                    with suppress(NotFound):
                        self._client.volumes.get(volume).remove(force=True)

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

    def _trust(self, user: str, claude_config: str, path: str) -> None:
        """Make the Claude config trust `path`, so no "Quick safety check" blocks the Remote Control
        Server.

        Trust of a parent directory is not inherited: it is registered for the exact path. The
        file is replaced atomically, mode 600, and only when the path is not trusted yet: other
        Projects of the same User may be running and writing it too.
        """
        exit_status = self._run_job(
            ["bash", "-c", TRUST_SCRIPT, "trust", path],
            volumes={claude_config: {"bind": CLAUDE_CONFIG_DIR, "mode": "rw"}},
            labels=_labels("claude-config-trust", user),
        )
        if exit_status != 0:
            raise RuntimeUnavailable(f"Could not trust {path}: the job exited with {exit_status}")

    def _ensure_network(self) -> str:
        """The Projects network, created on first use."""
        try:
            return str(self._client.networks.get(PROJECTS_NETWORK).name)
        except NotFound:
            pass
        try:
            self._client.networks.create(
                PROJECTS_NETWORK,
                driver="bridge",
                options={
                    "com.docker.network.bridge.enable_icc": "false",
                    "com.docker.network.bridge.name": PROJECTS_BRIDGE,
                },
                labels={KIND_LABEL: "projects-network"},
            )
        except APIError as error:
            if error.status_code != 409:  # created meanwhile by another start
                raise
        return PROJECTS_NETWORK

    def _ensure_home(self, user: str, project: str) -> str:
        """The Project's own home volume, created on first use."""
        name = _home_volume(project)
        try:
            self._client.volumes.get(name)
        except NotFound:
            labels = {**_labels("home", user), PROJECT_LABEL: project}
            volume = self._client.volumes.create(name, labels=labels)
            try:
                self._hand_to_non_root_user(name, labels={**labels, KIND_LABEL: "home-chown"})
            except RuntimeUnavailable:
                volume.remove(force=True)  # never left to root: the next start tries again
                raise
        return name

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


def _home_volume(project: str) -> str:
    return f"dockyard-home-{project}"


def _labels(kind: str, user: str) -> dict[str, str]:
    return {KIND_LABEL: kind, USER_LABEL: user}


def _unavailable(error: Exception) -> RuntimeUnavailable:
    host = os.environ.get("DOCKER_HOST", "the default Docker socket")
    return RuntimeUnavailable(
        f"Dockyard cannot start: Docker is unreachable at {host} ({error}). "
        "Dockyard talks to Docker only through the socket proxy: check that the socket proxy "
        "is running and that DOCKER_HOST points to it."
    )
