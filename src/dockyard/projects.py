"""Projects: which Projects a User may create, the status shown to them, and their lifecycle."""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import StrEnum
from urllib.parse import urlsplit

from .accounts import User
from .database import Database
from .git_credentials import GitCredentials
from .providers import Provider
from .runtime import (
    Clone,
    CloneState,
    GitToken,
    Output,
    ProjectMissing,
    ProjectNotRunning,
    ProjectStatus,
    Runtime,
    RuntimeUnavailable,
    Stats,
    Tty,
)

log = logging.getLogger(__name__)

NAME_MAX_LENGTH = 60
CLONE_POLL_INTERVAL = 1.0  # seconds between two looks at a clone or a sign-in in progress
CLONE_OUTPUT_LINES = 40  # the end of what git printed: its progress, or why it failed


PROVIDERS = {"github.com": Provider.GITHUB, "gitlab.com": Provider.GITLAB}
HOSTS = {provider: host for host, provider in PROVIDERS.items()}
# Seconds between two looks for Project environments that started without Dockyard.
GIT_CREDENTIALS_INTERVAL = 2.0
# A path segment of a repository: an owner, a group or the repository itself (never "." or "..").
SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


class Visibility(StrEnum):
    """A private Project needs a Git Credential; a public one may have one."""

    PUBLIC = "public"
    PRIVATE = "private"


class PermissionMode(StrEnum):
    """How much Claude may do in the Project without asking, chosen at creation."""

    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    AUTO = "auto"
    BYPASS_PERMISSIONS = "bypassPermissions"


class Status(StrEnum):
    """The status a User sees, worked out from what the Runtime holds."""

    CLONING = "cloning"
    WAITING = "waiting"  # it should run, but its owner's Claude Account is not connected
    RUNNING = "running"
    # Its Remote Control Server asks whether to trust the Project: its owner answers in the console.
    TRUST_PROMPT = "trust_prompt"
    STOPPED = "stopped"
    ERROR = "error"


_STATUS = {
    ProjectStatus.CLONING: Status.CLONING,
    ProjectStatus.RUNNING: Status.RUNNING,
    ProjectStatus.TRUST_PROMPT: Status.TRUST_PROMPT,
    ProjectStatus.FAILED: Status.ERROR,
    ProjectStatus.MISSING: Status.ERROR,
}


class Refusal(StrEnum):
    REPOSITORY_INVALID = "repository_invalid"
    REPOSITORY_NOT_HTTPS = "repository_not_https"
    REPOSITORY_CREDENTIALS = "repository_credentials"
    REPOSITORY_PORT = "repository_port"
    REPOSITORY_QUERY = "repository_query"
    REPOSITORY_FRAGMENT = "repository_fragment"
    REPOSITORY_HOST = "repository_host"
    NAME_MISSING = "name_missing"
    NAME_INVALID = "name_invalid"
    GIT_CREDENTIAL_REQUIRED = "git_credential_required"
    GIT_CREDENTIAL_NOT_FOUND = "git_credential_not_found"
    GIT_CREDENTIAL_PROVIDER = "git_credential_provider"
    CLONING = "cloning"
    NOT_RUNNING = "not_running"


class ProjectRefused(Exception):
    def __init__(self, reason: Refusal) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Repository:
    url: str
    provider: Provider


def parse_repository(text: str) -> Repository:
    """A repository on github.com or gitlab.com, by its plain https URL.

    Raises ProjectRefused.
    """
    text = text.strip()
    if not text or any(c.isspace() or not c.isprintable() or c == "\\" for c in text):
        raise ProjectRefused(Refusal.REPOSITORY_INVALID)
    url = urlsplit(text)
    if url.scheme.lower() != "https" or not text.lower().startswith("https://"):
        raise ProjectRefused(Refusal.REPOSITORY_NOT_HTTPS)
    if "@" in url.netloc:
        raise ProjectRefused(Refusal.REPOSITORY_CREDENTIALS)
    if ":" in url.netloc:
        raise ProjectRefused(Refusal.REPOSITORY_PORT)
    if "?" in text:
        raise ProjectRefused(Refusal.REPOSITORY_QUERY)
    if "#" in text:
        raise ProjectRefused(Refusal.REPOSITORY_FRAGMENT)
    host = url.netloc.lower()
    if host not in PROVIDERS:
        raise ProjectRefused(Refusal.REPOSITORY_HOST)
    provider = PROVIDERS[host]
    path = url.path.removesuffix("/")
    segments = path.split("/")[1:]
    # github.com: owner/repository. gitlab.com: group/project, or group/subgroups.../project.
    expected = len(segments) == 2 if provider == Provider.GITHUB else len(segments) >= 2
    if not expected or not all(SEGMENT.match(segment) for segment in segments):
        raise ProjectRefused(Refusal.REPOSITORY_INVALID)
    return Repository(url=f"https://{host}{path}", provider=provider)


def valid_name(name: str) -> str:
    """The name of a Project, which also names its Remote Control Server in the Claude app."""
    name = name.strip()
    if not name:
        raise ProjectRefused(Refusal.NAME_MISSING)
    if len(name) > NAME_MAX_LENGTH or not name.isprintable():
        raise ProjectRefused(Refusal.NAME_INVALID)
    return name


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    repository: str
    provider: Provider
    visibility: Visibility
    permission_mode: PermissionMode
    status: Status
    git_credential: str | None = None  # its id


@dataclass(frozen=True)
class CloneReport:
    """How a Project's clone went. A failed one outlives its Project, so its User can see why."""

    owner: str  # the owner's runtime id
    name: str
    repository: str
    visibility: Visibility
    git_credential: str | None
    state: CloneState = CloneState.RUNNING
    output: str = ""


class Projects:
    def __init__(
        self,
        database: Database,
        runtime: Runtime,
        git_credentials: GitCredentials,
        *,
        poll_interval: float = CLONE_POLL_INTERVAL,
    ) -> None:
        self._db = database
        self._runtime = runtime
        self._git_credentials = git_credentials
        self._poll_interval = poll_interval
        self._lock = threading.Lock()
        self._clones: dict[str, CloneReport] = {}  # since Dockyard started
        self._deleting: set[str] = set()
        # The start of each Project's environment its Git Credential was last written into.
        self._written: dict[str, str] = {}
        self._writing = threading.Lock()
        self._closed = threading.Event()

    def create(
        self,
        owner: User,
        *,
        name: str,
        repository: str,
        visibility: Visibility,
        permission_mode: PermissionMode = PermissionMode.DEFAULT,
        git_credential: str | None = None,
    ) -> Project:
        """Record the Project and start cloning it, with its Git Credential if it has one; once
        cloned, it starts (or waits for its owner's Claude Account).

        Raises ProjectRefused, RuntimeUnavailable.
        """
        name = valid_name(name)
        source = parse_repository(repository)
        self._check_git_credential(owner, git_credential, visibility, source.provider)
        try:
            with self._db.transaction() as conn:
                (project,) = conn.execute(
                    "INSERT INTO projects (owner, name, repository, provider, visibility,"
                    " permission_mode, git_credential) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
                    (
                        owner.runtime_id,
                        name,
                        source.url,
                        source.provider,
                        visibility,
                        permission_mode,
                        git_credential,
                    ),
                ).fetchone()
        except sqlite3.IntegrityError as error:  # its Git Credential was deleted meanwhile
            raise ProjectRefused(Refusal.GIT_CREDENTIAL_NOT_FOUND) from error
        credential = self._git_token(git_credential, source.provider)
        try:
            self._runtime.start_clone(owner.runtime_id, project, source.url, credential=credential)
        except RuntimeUnavailable:
            with suppress(RuntimeUnavailable):  # leave nothing behind, as far as Docker lets us
                self._runtime.remove_project(project, code=True)
            self._forget(project)
            raise
        self._follow(
            project, CloneReport(owner.runtime_id, name, source.url, visibility, git_credential)
        )
        return Project(
            project,
            name,
            source.url,
            source.provider,
            visibility,
            permission_mode,
            Status.CLONING,
            git_credential,
        )

    def list(self, owner: User) -> list[Project]:
        with self._db.transaction() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM projects WHERE owner = ? ORDER BY created_at, rowid",
                (owner.runtime_id,),
            ).fetchall()
        return [self._project(row) for row in rows]

    def get(self, owner: User, project: str) -> Project | None:
        row = self._row(owner, project)
        return self._project(row) if row else None

    def start(self, owner: User, project: str) -> Project | None:
        """Start the Project's Remote Control Server, restarting it if it runs, or have it wait
        for its owner's Claude Account. None if not found.

        Raises ProjectRefused, RuntimeUnavailable.
        """
        row = self._row(owner, project)
        if row is None:
            return None
        if self._runtime.project_status(project) == ProjectStatus.CLONING:
            raise ProjectRefused(Refusal.CLONING)
        self._start(row)
        return self.get(owner, project)

    def stop(self, owner: User, project: str) -> Project | None:
        """Stop the Project's Remote Control Server, or its wait. None if not found.

        Raises RuntimeUnavailable.
        """
        if self._row(owner, project) is None:
            return None
        self._should_run(project, False)
        self._runtime.stop_project(project)
        return self.get(owner, project)

    def follow_log(self, owner: User, project: str) -> Output | None:
        """What the Project's Remote Control Server printed, then what it prints, until it stops
        or restarts. None if not found.

        Raises ProjectRefused, RuntimeUnavailable.
        """
        return self._while_running(owner, project, self._runtime.follow_project_log)

    def attach_console(self, owner: User, project: str) -> Tty | None:
        """The console: the Project's Remote Control Server's own TTY. None if not found.

        Raises ProjectRefused, RuntimeUnavailable.
        """
        return self._while_running(owner, project, self._runtime.attach_project)

    def open_terminal(self, owner: User, project: str) -> Tty | None:
        """A Terminal: a new shell of the non-root user in the Project. None if not found.

        Raises ProjectRefused, RuntimeUnavailable.
        """
        return self._while_running(owner, project, self._runtime.open_terminal)

    def stats(self, owner: User, project: str) -> Stats | None:
        """The CPU and RAM the Project uses now. None if not found.

        Raises ProjectRefused, RuntimeUnavailable.
        """
        if self._row(owner, project) is None:
            return None
        stats = self._runtime.project_stats(project)
        if stats is None:
            raise ProjectRefused(Refusal.NOT_RUNNING)
        return stats

    def follow_sign_in(self, owner: User) -> None:
        """Once the owner's sign-in ends, start their Projects waiting for their Claude Account."""
        self._follow_sign_in_in_background(owner.runtime_id)

    def keep_git_credentials(self) -> None:
        """Until close: hand their Git Credential to the Projects whose environment started
        without Dockyard, such as after their server crashed or Docker restarted, when it lost
        what was written at the previous start."""
        threading.Thread(
            target=self._keep_git_credentials, name="git-credentials", daemon=True
        ).start()

    def close(self) -> None:
        """Dockyard is stopping: stop following sign-ins and keeping Git Credentials."""
        self._closed.set()

    def clone(self, owner: User, project: str) -> CloneReport | None:
        """How the Project's clone went, if it was cloned since Dockyard started."""
        with self._lock:
            report = self._clones.get(project)
        return report if report and report.owner == owner.runtime_id else None

    def delete(self, owner: User, project: str, *, code: bool) -> bool:
        """Remove the Project and its environment; its code only if `code`. False if not found."""
        if self.get(owner, project) is None:
            return False
        with self._lock:
            # Its clone, removed meanwhile, must not pass for a failed one: that removes the code.
            self._deleting.add(project)
        try:
            self._runtime.remove_project(project, code=code)
            self._forget(project)
            with self._lock:
                self._clones.pop(project, None)
                self._written.pop(project, None)
        finally:
            with self._lock:
                self._deleting.discard(project)
        return True

    def resume(self) -> None:
        """Pick up what was in progress when Dockyard stopped: clones, and Projects waiting for
        a Claude Account (connected meanwhile, or with a sign-in in progress)."""
        with self._db.transaction() as conn:
            rows = conn.execute(f"SELECT {_COLUMNS}, owner FROM projects").fetchall()
            waiting = conn.execute(
                "SELECT DISTINCT owner FROM projects WHERE should_run = 1"
            ).fetchall()
        for row in rows:
            if self._runtime.project_status(row["id"]) == ProjectStatus.CLONING:
                self._follow(
                    row["id"],
                    CloneReport(
                        row["owner"],
                        row["name"],
                        row["repository"],
                        Visibility(row["visibility"]),
                        row["git_credential"],
                    ),
                )
        for (owner,) in waiting:
            if self._runtime.login_environment_running(owner):
                self._follow_sign_in_in_background(owner)
            else:
                self._start_waiting(owner)

    def _check_git_credential(
        self, owner: User, credential: str | None, visibility: Visibility, provider: Provider
    ) -> None:
        """A private Project needs a Git Credential; any Project takes only one of its owner's,
        of its repository's provider. Raises ProjectRefused."""
        if credential is None:
            if visibility == Visibility.PRIVATE:
                raise ProjectRefused(Refusal.GIT_CREDENTIAL_REQUIRED)
            return
        found = self._git_credentials.get(owner, credential)
        if found is None:
            raise ProjectRefused(Refusal.GIT_CREDENTIAL_NOT_FOUND)
        if found.provider != provider:
            raise ProjectRefused(Refusal.GIT_CREDENTIAL_PROVIDER)

    def _git_token(self, credential: str | None, provider: Provider) -> GitToken | None:
        """The Git Credential's token, for the host of the provider it was checked against."""
        token = self._git_credentials.token(credential) if credential else None
        return GitToken(HOSTS[provider], token) if token else None

    def _while_running[T: Output](
        self, owner: User, project: str, opening: Callable[[str], T]
    ) -> T | None:
        """What `opening` opens on the owner's running Project. None if not found.

        Raises ProjectRefused, RuntimeUnavailable.
        """
        if self._row(owner, project) is None:
            return None
        try:
            return opening(project)
        except ProjectNotRunning as error:
            raise ProjectRefused(Refusal.NOT_RUNNING) from error

    def _start(self, row: sqlite3.Row) -> None:
        """Start its Remote Control Server, or have it wait for its owner's Claude Account."""
        if self._runtime.claude_account_connected(row["owner"]):
            with suppress(ProjectMissing):  # its status says so
                self._runtime.start_project(
                    row["owner"],
                    row["id"],
                    name=row["name"],
                    permission_mode=row["permission_mode"],
                )
                if row["git_credential"]:
                    start = self._runtime.project_starts().get(row["id"])
                    self._write_git_credential(row, start)
        self._should_run(row["id"], True)

    def _keep_git_credentials(self) -> None:
        while not self._closed.wait(GIT_CREDENTIALS_INTERVAL):
            try:
                starts = self._runtime.project_starts()
                with self._db.transaction() as conn:
                    rows = conn.execute(
                        "SELECT id, provider, git_credential FROM projects"
                        " WHERE git_credential IS NOT NULL"
                    ).fetchall()
                for row in rows:
                    if (start := starts.get(row["id"])) is not None:
                        self._write_git_credential(row, start)
            except RuntimeUnavailable as error:
                log.warning("Cannot hand Projects their Git Credential: %s", error)
            except Exception:  # never the end of it: Projects would lose theirs at the next crash
                log.exception("Cannot hand Projects their Git Credential")

    def _write_git_credential(self, row: sqlite3.Row, start: str | None) -> None:
        """Write the Project's Git Credential into its environment, as of its `start`, unless
        it is there already: written again by _keep_git_credentials if it failed, or if the
        environment starts again. One write at a time: each replaces what the last wrote."""
        if start is None:
            return
        with self._writing:
            with self._lock:
                if self._written.get(row["id"]) == start:
                    return
            credential = self._git_token(row["git_credential"], Provider(row["provider"]))
            if credential is None:
                return
            try:
                self._runtime.write_git_credential(row["id"], credential)
            except ProjectNotRunning:
                return  # its server stopped meanwhile: written when it starts again
            except RuntimeUnavailable as error:
                log.warning("Cannot hand Project %s its Git Credential: %s", row["id"], error)
                return
            with self._lock:
                self._written[row["id"]] = start

    def _start_waiting(self, owner: str) -> None:
        """Start the owner's Projects that wait for their Claude Account, if it is connected."""
        with self._db.transaction() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS}, owner FROM projects WHERE owner = ? AND should_run = 1",
                (owner,),
            ).fetchall()
        stopped = ProjectStatus.STOPPED
        waiting = [row for row in rows if self._runtime.project_status(row["id"]) == stopped]
        if waiting and self._runtime.claude_account_connected(owner):
            for row in waiting:
                self._start(row)

    def _follow_sign_in_in_background(self, owner: str) -> None:
        threading.Thread(
            target=self._follow_sign_in, args=(owner,), name=f"sign-in-{owner}", daemon=True
        ).start()

    def _follow_sign_in(self, owner: str) -> None:
        """Until the sign-in ends, then start the Projects waiting for it: retried until Docker
        answers, or the waiting Projects would wait for an account already connected."""
        while not self._closed.wait(self._poll_interval):
            try:
                if not self._runtime.login_environment_running(owner):
                    self._start_waiting(owner)
                    return
            except RuntimeUnavailable as error:
                log.warning("Cannot follow the sign-in of %s: %s", owner, error)

    def _follow(self, project: str, report: CloneReport) -> None:
        with self._lock:
            self._clones[project] = report
        threading.Thread(
            target=self._follow_clone, args=(project,), name=f"clone-{project}", daemon=True
        ).start()

    def _follow_clone(self, project: str) -> None:
        """Until the clone ends: report its output, then keep its code, or remove what it left."""
        while True:
            try:
                clone = self._runtime.clone(project)
                if clone is None:  # the Project was deleted
                    return
                with self._lock:
                    deleting = project in self._deleting
                if not deleting:
                    if clone.state != CloneState.RUNNING:
                        self._conclude(project, clone)
                        return
                    self._report(project, clone)
            except RuntimeUnavailable as error:
                log.warning("Cannot follow the clone of Project %s: %s", project, error)
            time.sleep(self._poll_interval)

    def _conclude(self, project: str, clone: Clone) -> None:
        """Deal with how the clone ended, then report it: a failed one means its Project is gone."""
        if clone.state == CloneState.SUCCEEDED:
            # Started while its clone is still there: it goes from cloning to running at once.
            if row := self._row_by_id(project):
                self._start(row)
            self._runtime.finish_clone(project)
        else:
            self._remove_failed(project)
        self._report(project, clone)

    def _remove_failed(self, project: str) -> None:
        """A failed clone leaves nothing behind: retried until Docker lets it all go."""
        while True:
            try:
                self._runtime.remove_project(project, code=True)
                break
            except RuntimeUnavailable as error:
                log.warning("Cannot remove the failed clone of Project %s: %s", project, error)
                time.sleep(self._poll_interval)
        self._forget(project)

    def _report(self, project: str, clone: Clone) -> None:
        with self._lock:
            if project in self._clones:  # not deleted meanwhile
                self._clones[project] = replace(
                    self._clones[project], state=clone.state, output=_readable(clone.output)
                )

    def _row(self, owner: User, project: str) -> sqlite3.Row | None:
        with self._db.transaction() as conn:
            row: sqlite3.Row | None = conn.execute(
                f"SELECT {_COLUMNS}, owner FROM projects WHERE owner = ? AND id = ?",
                (owner.runtime_id, project),
            ).fetchone()
        return row

    def _row_by_id(self, project: str) -> sqlite3.Row | None:
        with self._db.transaction() as conn:
            row: sqlite3.Row | None = conn.execute(
                f"SELECT {_COLUMNS}, owner FROM projects WHERE id = ?", (project,)
            ).fetchone()
        return row

    def _should_run(self, project: str, should_run: bool) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE projects SET should_run = ? WHERE id = ?", (should_run, project))

    def _forget(self, project: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM projects WHERE id = ?", (project,))

    def _project(self, row: sqlite3.Row) -> Project:
        found = self._runtime.project_status(row["id"])
        if found == ProjectStatus.STOPPED:
            status = Status.WAITING if row["should_run"] else Status.STOPPED
        else:
            status = _STATUS[found]
        return Project(
            id=row["id"],
            name=row["name"],
            repository=row["repository"],
            provider=Provider(row["provider"]),
            visibility=Visibility(row["visibility"]),
            permission_mode=PermissionMode(row["permission_mode"]),
            status=status,
            git_credential=row["git_credential"],
        )


_COLUMNS = "id, name, repository, provider, visibility, permission_mode, should_run, git_credential"


def _readable(output: str) -> str:
    """git's output as a terminal would leave it: each progress line shows its last update."""
    lines = [line.rstrip("\r").rsplit("\r", 1)[-1] for line in output.rstrip("\n").split("\n")]
    return "\n".join(lines[-CLONE_OUTPUT_LINES:])
