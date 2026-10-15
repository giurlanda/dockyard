"""Docker adapter. It reaches Docker only through DOCKER_HOST, which points to the socket proxy."""

from __future__ import annotations

import http.client
import os
import secrets
import socket
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from typing import Any

import docker
import requests
import urllib3
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.models.containers import Container

from . import (
    CLONE_TIMEOUT,
    DEFAULT_DNS,
    LOGIN_TIMEOUT,
    PROJECT_CPUS,
    PROJECT_LOG_LINES,
    PROJECT_MEMORY,
    PROJECT_PIDS_LIMIT,
    TRUST_PROMPT,
    Clone,
    CloneState,
    GitToken,
    LoginNotRunning,
    ProjectMissing,
    ProjectNotRunning,
    ProjectStatus,
    RuntimeUnavailable,
    Stats,
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
VOLUME_RELEASE_TIMEOUT = 10  # seconds a container being removed may still hold its volumes
VOLUME_RELEASE_INTERVAL = 0.2  # seconds between two attempts to remove such a volume
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
# Whether the Claude config trusts the path $1.
TRUSTED_SCRIPT = """
jq -e --arg p "$1" '.projects[$p].hasTrustDialogAccepted == true' \\
    "$CLAUDE_CONFIG_DIR/.claude.json" >/dev/null
"""
# A Terminal: a login shell in the Project's path $1, never set as the working directory (see
# WORKSPACE), named $2 so that it can be hung up.
TERMINAL_SCRIPT = 'cd -- "$1" && exec -a "$2" bash -l'
# Hangs up the Terminal named $1: its shell leads a session of its own, and what it runs is in it.
# Docker leaves the shell running when the connection to it closes.
HANG_UP_SCRIPT = 'for shell in $(pgrep -f -x -- "$1 -l"); do pkill -HUP -s "$shell"; done'
# Clones the repository $1 into $2. With a Git Credential (the git user name for its host in
# $3), its token comes first on stdin and reaches git through a credential helper that reads it
# from this script's environment: never in a URL, never stored in the repository.
CLONE_SCRIPT = f"""
set -eu
helpers=(-c credential.helper=)
if [ -n "$3" ]; then
  if ! IFS= read -r -t 60 DOCKYARD_TOKEN; then
    echo "fatal: the Git Credential did not arrive" >&2
    exit 1
  fi
  export DOCKYARD_TOKEN DOCKYARD_USERNAME="$3"
  helpers+=(-c 'credential.helper=!f() {{
    echo "username=$DOCKYARD_USERNAME"; echo "password=$DOCKYARD_TOKEN"; }}; f')
fi
exec timeout {CLONE_TIMEOUT} git "${{helpers[@]}}" clone --progress -- "$1" "$2"
"""
CLONE_USERNAMES = {"github.com": "x-access-token", "gitlab.com": "oauth2"}
# Where a Project's Git Credential lives: a tmpfs, emptied at every start of its environment.
SECRETS = "/run/dockyard"
SECRETS_TMPFS = f"uid={UID},gid={UID},mode=0700,size=1m"
GH_CONFIG_DIR = f"{SECRETS}/gh"
GLAB_CONFIG_DIR = f"{SECRETS}/glab"
# Writes the token read on stdin as the Git Credential for the host $1, in the shapes gh and glab
# read (validated in Phase 0), then makes them git's credential helpers for that host. Whatever
# was written before goes first.
WRITE_GIT_CREDENTIAL_SCRIPT = """
set -eu
IFS= read -r token
umask 077
rm -rf -- "$GH_CONFIG_DIR" "$GLAB_CONFIG_DIR"
case "$1" in
  github.com)
    mkdir -p "$GH_CONFIG_DIR"
    printf 'github.com:\\n    oauth_token: %s\\n    git_protocol: https\\n    user: dockyard\\n' \\
        "$token" > "$GH_CONFIG_DIR/hosts.yml"
    gh auth setup-git --hostname github.com
    ;;
  gitlab.com)
    mkdir -p "$GLAB_CONFIG_DIR"
    printf '%s\\n' 'check_update: false' 'hosts:' '    gitlab.com:' "        token: $token" \\
        '        api_host: gitlab.com' '        git_protocol: https' \\
        > "$GLAB_CONFIG_DIR/config.yml"
    git config --global --unset-all credential.https://gitlab.com.helper || true
    git config --global --add credential.https://gitlab.com.helper ''
    git config --global --add credential.https://gitlab.com.helper '!glab auth git-credential'
    ;;
  *) echo "Unknown host: $1" >&2; exit 2 ;;
esac
"""
WRITE_TIMEOUT = 30  # seconds
WRITE_INTERVAL = 0.05  # seconds between two looks at a write in progress
HANG_UP_ATTEMPTS = 20
HANG_UP_INTERVAL = 0.25  # seconds
# How far back a Project's log is searched for a trust prompt still waiting: nothing is printed
# while it waits.
TRUST_PROMPT_LOG_LINES = 50

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

    def remove_claude_account(self, user: str) -> None:
        with self._docker():
            self.stop_login_environment(user)
            try:
                claude_config = self._client.volumes.get(_claude_config_volume(user))
            except NotFound:
                return
            # A login environment Docker is still removing holds the volume for a moment.
            deadline = time.monotonic() + VOLUME_RELEASE_TIMEOUT
            while True:
                try:
                    claude_config.remove(force=True)
                    return
                except NotFound:
                    return
                except APIError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(VOLUME_RELEASE_INTERVAL)

    def start_clone(
        self, user: str, project: str, repository: str, *, credential: GitToken | None = None
    ) -> None:
        path = project_path(project)
        labels = {**_labels(CLONE, user), PROJECT_LABEL: project}
        username = CLONE_USERNAMES[credential.host] if credential else ""
        with self._docker():
            code = self._client.volumes.create(
                _code_volume(project), labels={**_labels("code", user), PROJECT_LABEL: project}
            ).name
            self._hand_to_non_root_user(code, labels={**labels, KIND_LABEL: "code-chown"})
            clone = self._client.containers.create(
                self._base_image,
                ["bash", "-c", CLONE_SCRIPT, "clone", repository, path, username],
                name=f"dockyard-{CLONE}-{project}",
                volumes={code: {"bind": path, "mode": "rw"}},
                working_dir=WORKSPACE,
                # A repository that is not public asks for a user name: fail instead of waiting.
                environment={"GIT_TERMINAL_PROMPT": "0"},
                labels=labels,
                stdin_open=credential is not None,  # where its token arrives
                init=True,
                mem_limit="1g",
                nano_cpus=1_000_000_000,
                **HARDENING,
            )
            clone.start()
            if credential is not None:
                stdin = self._client.api.attach_socket(
                    str(clone.id), params={"stdin": 1, "stream": 1}
                )
                with _sending(stdin) as send:
                    send(credential.token)

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
                # Where gh and glab look for the Git Credential: see write_git_credential.
                tmpfs={SECRETS: SECRETS_TMPFS},
                environment={
                    "TERM": "xterm-256color",
                    "GH_CONFIG_DIR": GH_CONFIG_DIR,
                    "GLAB_CONFIG_DIR": GLAB_CONFIG_DIR,
                },
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

    def follow_project_log(self, project: str) -> FollowedLog:
        with self._docker():
            environments = self._project_containers(project, PROJECT_ENVIRONMENT)
            if not environments:
                raise ProjectNotRunning(project)
            return FollowedLog(self._client.api, str(environments[0].id))

    def attach_project(self, project: str) -> AttachedTty:
        with self._docker():
            environment = self._running_environment(project)
            # Its last lines only: what it printed since its environment started can be a lot.
            replay = environment.logs(tail=PROJECT_LOG_LINES)
            try:
                return AttachedTty(self._client.api, str(environment.id), replay=replay)
            except (NotFound, APIError) as error:  # it stopped or restarts meanwhile
                raise ProjectNotRunning(project) from error

    def open_terminal(self, project: str) -> Terminal:
        with self._docker():
            environment = str(self._running_environment(project).id)
            name = f"dockyard-terminal-{secrets.token_hex(6)}"
            try:
                shell = self._client.api.exec_create(
                    environment,
                    ["bash", "-c", TERMINAL_SCRIPT, "dockyard", project_path(project), name],
                    stdin=True,
                    tty=True,
                    user=f"{UID}:{UID}",
                    environment={"TERM": "xterm-256color"},
                )
                return Terminal(self._client.api, environment, str(shell["Id"]), name)
            except (NotFound, APIError) as error:  # it stopped or restarts meanwhile
                raise ProjectNotRunning(project) from error

    def write_git_credential(self, project: str, credential: GitToken) -> None:
        with self._docker():
            environment = str(self._running_environment(project).id)
            try:
                writer = self._client.api.exec_create(
                    environment,
                    ["bash", "-c", WRITE_GIT_CREDENTIAL_SCRIPT, "git-credential", credential.host],
                    stdin=True,
                    user=f"{UID}:{UID}",
                )
                writing = str(writer["Id"])
                # On stdin: an exec's command and environment are in Docker's metadata.
                stdin = self._client.api.exec_start(writing, socket=True)
            except (NotFound, APIError) as error:  # it stopped or restarts meanwhile
                raise ProjectNotRunning(project) from error
            with _sending(stdin) as send:  # open until it ends: it may print
                send(credential.token)
                deadline = time.monotonic() + WRITE_TIMEOUT
                while (written := self._client.api.exec_inspect(writing))["Running"]:
                    if time.monotonic() > deadline:
                        raise RuntimeUnavailable(f"Writing the Git Credential of {project} hangs")
                    time.sleep(WRITE_INTERVAL)
            if written["ExitCode"] != 0:
                raise RuntimeUnavailable(
                    f"Could not write the Git Credential of {project}: "
                    f"the writer exited with {written['ExitCode']}"
                )

    def project_starts(self) -> dict[str, str]:
        with self._docker():
            environments = self._client.containers.list(
                filters={"label": f"{KIND_LABEL}={PROJECT_ENVIRONMENT}", "status": "running"}
            )
            return {
                environment.labels[PROJECT_LABEL]: environment.attrs["State"]["StartedAt"]
                for environment in environments
            }

    def project_stats(self, project: str) -> Stats | None:
        with self._docker():
            try:
                environment = self._running_environment(project)
                # Two samples, a second apart: CPU usage is the difference between them.
                sample: dict[str, Any] = self._client.api.stats(str(environment.id), stream=False)
            except (ProjectNotRunning, NotFound):
                return None
        cpu, before = sample["cpu_stats"], sample["precpu_stats"]
        used = cpu["cpu_usage"]["total_usage"] - before["cpu_usage"]["total_usage"]
        elapsed = cpu.get("system_cpu_usage", 0) - before.get("system_cpu_usage", 0)
        if not cpu.get("online_cpus") or elapsed <= 0:
            return None  # it stopped meanwhile: Docker sends empty samples
        memory = sample["memory_stats"]
        # As `docker stats` counts it: the page cache it can drop at once is not in use.
        cache = memory.get("stats", {}).get("inactive_file", 0)
        return Stats(
            cpu=used / elapsed * cpu["online_cpus"] * 100,
            cpus=environment.attrs["HostConfig"]["NanoCpus"] / 1_000_000_000,
            memory=memory["usage"] - cache,
            memory_limit=memory["limit"],
        )

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
            if environments[0].status != "running":
                return ProjectStatus.FAILED
            if self._asks_for_trust(environments[0], project_path(project)):
                return ProjectStatus.TRUST_PROMPT
            return ProjectStatus.RUNNING

    def remove_project(self, project: str, *, code: bool) -> None:
        with self._docker():
            for container in self._project_containers(project):
                with suppress(NotFound):
                    container.remove(force=True)
            if code:
                for volume in (_code_volume(project), _home_volume(project)):
                    with suppress(NotFound):
                        self._client.volumes.get(volume).remove(force=True)

    def _running_environment(self, project: str) -> Container:
        """Raises ProjectNotRunning."""
        environments = self._project_containers(project, PROJECT_ENVIRONMENT)
        if not environments or environments[0].status != "running":
            raise ProjectNotRunning(project)
        return environments[0]

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

    def _asks_for_trust(self, environment: Container, path: str) -> bool:
        """Whether its server shows the trust prompt, not answered yet.

        The prompt stays in the log once answered: the Claude config tells. It is only read when
        the prompt is there, which only happens when the trust registered before the start was
        lost.
        """
        log = environment.logs(tail=TRUST_PROMPT_LOG_LINES).decode(errors="replace")
        if TRUST_PROMPT not in log:
            return False
        user = environment.labels[USER_LABEL]
        exit_status = self._run_job(
            ["bash", "-c", TRUSTED_SCRIPT, "trusted", path],
            volumes={_claude_config_volume(user): {"bind": CLAUDE_CONFIG_DIR, "mode": "ro"}},
            labels=_labels("claude-config-check", user),
        )
        return exit_status != 0

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


class _HijackedTty:
    """A TTY reached through a connection Docker hijacked, starting with `replay`."""

    def __init__(self, stream: Any, *, replay: bytes = b"") -> None:
        self._response = stream._response
        self._socket: socket.socket = stream._sock
        self._unread = replay + _read_ahead(self._response, self._socket)
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

    def close(self) -> None:
        with suppress(OSError):  # wakes up a read() waiting in another thread
            self._socket.shutdown(socket.SHUT_RDWR)
        self._socket.close()
        self._response.close()


class AttachedTty(_HijackedTty):
    """The TTY of a running container's main process, through Docker's attach.

    It starts with everything the program printed, or with `replay` instead.
    """

    def __init__(
        self, api: docker.APIClient, container: str, *, replay: bytes | None = None
    ) -> None:
        self._api = api
        self._container = container
        logs = 1 if replay is None else 0
        stream = api.attach_socket(
            container, params={"stdin": 1, "stdout": 1, "stderr": 1, "stream": 1, "logs": logs}
        )
        super().__init__(stream, replay=replay or b"")

    def resize(self, rows: int, cols: int) -> None:
        with suppress(DockerException, requests.RequestException):  # it ended: read() says so
            self._api.resize(self._container, height=rows, width=cols)


class Terminal(_HijackedTty):
    """A Terminal's shell, created with Docker's exec as `name`, started here."""

    def __init__(self, api: docker.APIClient, container: str, shell: str, name: str) -> None:
        self._api = api
        self._container = container
        self._shell = shell
        self._name = name
        super().__init__(api.exec_start(shell, tty=True, socket=True))

    def resize(self, rows: int, cols: int) -> None:
        with suppress(DockerException, requests.RequestException):  # it ended: read() says so
            self._api.exec_resize(self._shell, height=rows, width=cols)

    def close(self) -> None:
        """Lets go of the shell at once, and hangs it up meanwhile."""
        super().close()
        threading.Thread(target=self._hang_up, name=self._name, daemon=True).start()

    def _hang_up(self) -> None:
        """Until its shell has ended: one closed right after it opened may not bear its name yet."""
        with suppress(DockerException, requests.RequestException):  # its environment is gone
            for _ in range(HANG_UP_ATTEMPTS):
                if not self._api.exec_inspect(self._shell)["Running"]:
                    return
                hang_up = self._api.exec_create(
                    self._container,
                    ["bash", "-c", HANG_UP_SCRIPT, "hang-up", self._name],
                    user=f"{UID}:{UID}",
                )
                self._api.exec_start(hang_up["Id"])
                time.sleep(HANG_UP_INTERVAL)


class FollowedLog:
    """A container's log, followed through Docker's logs endpoint.

    Read straight from the HTTP response, which takes Docker's chunks apart as they come: the
    stream docker-py offers hands a TTY's log over one byte at a time.
    """

    def __init__(self, api: docker.APIClient, container: str) -> None:
        client: Any = api  # its helpers for raw responses are not in its stubs
        self._stream = client.logs(container, stream=True, follow=True, tail=PROJECT_LOG_LINES)
        self._response = self._stream._response
        # docker-py leaves the client's request timeout on it: a quiet server is no failure.
        client._disable_socket_timeout(client._get_raw_response_socket(self._response))

    def read(self) -> bytes:
        try:
            data = bytes(self._response.raw.read1(65536))
        except (OSError, ValueError, http.client.HTTPException, urllib3.exceptions.HTTPError):
            data = b""  # closed meanwhile: the chunk it was reading is cut short
        if not data:
            self._response.close()
        return data

    def close(self) -> None:
        """Only shuts the connection down: the reader lets go of the response once it sees that."""
        with suppress(OSError, DockerException):
            self._stream.close()


@contextmanager
def _sending(stream: Any) -> Iterator[Callable[[str], None]]:
    """Sends lines to a program's stdin, through a connection Docker hijacked; closes it after."""
    try:
        yield lambda line: stream._sock.sendall(f"{line}\n".encode())
    finally:
        stream._sock.close()
        stream._response.close()


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
