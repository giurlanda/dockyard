"""Projects: which Projects a User may create, the status shown to them, and their lifecycle."""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext, suppress
from dataclasses import dataclass, replace
from enum import StrEnum
from urllib.parse import urlsplit

from .accounts import RemovedUser, User
from .database import Database
from .git_credentials import GitCredentials
from .providers import Provider
from .runtime import (
    PROJECT_CPUS,
    PROJECT_MEMORY,
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
# The least a Project may be given, whatever the Instance limits: Claude Code needs it to run.
MIN_CPUS = 0.5
GIB = 1024**3  # bytes
MIN_MEMORY = GIB  # bytes
MAX_ACTIVE_PROJECTS = 3  # per User, unless the Instance configuration says otherwise


@dataclass(frozen=True)
class Limits:
    """The Instance limits: the most CPU and RAM one Project may use, which is also what it gets
    unless its User chooses less, and how many active Projects each User may have."""

    max_cpus: float = PROJECT_CPUS
    max_memory: int = PROJECT_MEMORY  # bytes
    max_active: int = MAX_ACTIVE_PROJECTS


DEFAULT_LIMITS = Limits()


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
    CPUS_OUT_OF_RANGE = "cpus_out_of_range"
    MEMORY_OUT_OF_RANGE = "memory_out_of_range"
    ACTIVE_MAXIMUM = "active_maximum"
    PRIVILEGED_NOT_PERMITTED = "privileged_not_permitted"
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
    cpus: float = PROJECT_CPUS  # the CPUs it may use
    memory: int = PROJECT_MEMORY  # the RAM it may use, in bytes
    # Its environment runs privileged, which equals root on the host: only while its owner
    # holds the permission to create Privileged Projects.
    privileged: bool = False


@dataclass(frozen=True)
class OwnedProject:
    """One of the Projects on the Instance, as an Administrator sees it."""

    owner: str  # its owner's email
    project: Project


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
    cpus: float | None = None
    memory: int | None = None  # bytes


class Projects:
    def __init__(
        self,
        database: Database,
        runtime: Runtime,
        git_credentials: GitCredentials,
        *,
        limits: Limits = DEFAULT_LIMITS,
        poll_interval: float = CLONE_POLL_INTERVAL,
    ) -> None:
        self.limits = limits
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
        # Held while a Privileged Project starts, and while those of an owner whose permission
        # was revoked are stopped: none starts privileged once its owner's are stopped.
        self._starting_privileged = threading.Lock()
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
        cpus: float | None = None,
        memory: int | None = None,
        privileged: bool = False,
    ) -> Project:
        """Record the Project and start cloning it, with its Git Credential if it has one; once
        cloned, it starts (or waits for its owner's Claude Account). It may use `cpus` CPUs and
        `memory` bytes of RAM, within the Instance limits: their maximum when not given. It is
        active from now on, so the owner must have fewer active Projects than the maximum. A
        `privileged` one needs its owner to hold the permission to create Privileged Projects.

        Raises ProjectRefused, RuntimeUnavailable.
        """
        name = valid_name(name)
        source = parse_repository(repository)
        cpus = self.limits.max_cpus if cpus is None else cpus
        memory = self.limits.max_memory if memory is None else memory
        if not MIN_CPUS <= cpus <= self.limits.max_cpus:
            raise ProjectRefused(Refusal.CPUS_OUT_OF_RANGE)
        if not MIN_MEMORY <= memory <= self.limits.max_memory:
            raise ProjectRefused(Refusal.MEMORY_OUT_OF_RANGE)
        self._check_git_credential(owner, git_credential, visibility, source.provider)
        if privileged and not self._permitted(owner.runtime_id):
            raise ProjectRefused(Refusal.PRIVILEGED_NOT_PERMITTED)
        try:
            with self._db.transaction() as conn:
                # Counted and recorded in one statement: two creations at once cannot both pass.
                created = conn.execute(
                    "INSERT INTO projects (owner, name, repository, provider, visibility,"
                    " permission_mode, git_credential, cpus, memory, privileged, should_run)"
                    " SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1"
                    f" WHERE ({_ACTIVE}) < ? RETURNING id",
                    (
                        owner.runtime_id,
                        name,
                        source.url,
                        source.provider,
                        visibility,
                        permission_mode,
                        git_credential,
                        cpus,
                        memory,
                        privileged,
                        owner.runtime_id,
                        self.limits.max_active,
                    ),
                ).fetchone()
        except sqlite3.IntegrityError as error:  # its Git Credential was deleted meanwhile
            raise ProjectRefused(Refusal.GIT_CREDENTIAL_NOT_FOUND) from error
        if created is None:
            raise ProjectRefused(Refusal.ACTIVE_MAXIMUM)
        (project,) = created
        credential = self._git_token(git_credential, source.provider)
        try:
            self._runtime.start_clone(owner.runtime_id, project, source.url, credential=credential)
        except RuntimeUnavailable:
            with suppress(RuntimeUnavailable):  # leave nothing behind, as far as Docker lets us
                self._runtime.remove_project(project, code=True)
            self._forget(project)
            raise
        self._follow(
            project,
            CloneReport(
                owner.runtime_id,
                name,
                source.url,
                visibility,
                git_credential,
                cpus=cpus,
                memory=memory,
            ),
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
            cpus,
            memory,
            privileged,
        )

    def list_all(self) -> list[OwnedProject]:
        """Every Project on the Instance, with its owner: for Administrators only."""
        with self._db.transaction() as conn:
            rows = conn.execute(f"{_OWNED} ORDER BY owner_email, created_at, rowid").fetchall()
        return [self._owned(row) for row in rows]

    def list(self, owner: User) -> list[Project]:
        with self._db.transaction() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM projects WHERE owner = ? ORDER BY created_at, rowid",
                (owner.runtime_id,),
            ).fetchall()
        return [self._project(row) for row in rows]

    def get_any(self, project: str) -> OwnedProject | None:
        """Any Project on the Instance, with its owner: for Administrators only."""
        with self._db.transaction() as conn:
            row = conn.execute(f"{_OWNED} WHERE id = ?", (project,)).fetchone()
        return self._owned(row) if row else None

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
        if row["privileged"] and not self._permitted(owner.runtime_id):
            raise ProjectRefused(Refusal.PRIVILEGED_NOT_PERMITTED)
        with self._db.transaction() as conn:
            # Counted and recorded in one statement: two starts at once cannot both pass.
            active = conn.execute(
                "UPDATE projects SET should_run = 1"
                f" WHERE id = ? AND (should_run = 1 OR ({_ACTIVE}) < ?)",
                (project, owner.runtime_id, self.limits.max_active),
            ).rowcount
        if not active:
            raise ProjectRefused(Refusal.ACTIVE_MAXIMUM)
        try:
            self._start(row)
        except RuntimeUnavailable:
            if not row["should_run"]:  # it was not active: it still is not
                self._should_run(project, False)
            raise
        return self.get(owner, project)

    def stop(self, owner: User, project: str) -> Project | None:
        """Stop the Project's Remote Control Server, or its wait. None if not found.

        Raises RuntimeUnavailable.
        """
        if self._row(owner, project) is None:
            return None
        self._stop(project)
        return self.get(owner, project)

    def stop_any(self, project: str) -> OwnedProject | None:
        """Stop any Project on the Instance, as its owner would: for Administrators only. None
        if not found.

        Raises RuntimeUnavailable.
        """
        if self._row_by_id(project) is None:
            return None
        self._stop(project)
        return self.get_any(project)

    def stop_privileged(self, owner: User) -> None:
        """Stop the owner's Privileged Projects: their permission was revoked, so they cannot
        start again until it is granted again. Retried in the background until Docker lets them
        all stop."""
        try:
            self._stop_privileged(owner.runtime_id)
        except RuntimeUnavailable as error:
            log.warning("Cannot stop the Privileged Projects of a User yet: %s", error)
            self._stop_privileged_in_background(owner.runtime_id)

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
        return self._stats(project)

    def stats_any(self, project: str) -> Stats | None:
        """The CPU and RAM any Project on the Instance uses now: for Administrators only. None
        if not found.

        Raises ProjectRefused, RuntimeUnavailable.
        """
        if self._row_by_id(project) is None:
            return None
        return self._stats(project)

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
        if self._row(owner, project) is None:
            return False
        self._delete(project, code=code)
        return True

    def delete_any(self, project: str, *, code: bool) -> bool:
        """Delete any Project on the Instance, as its owner would: for Administrators only.
        False if not found.
        """
        if self._row_by_id(project) is None:
            return False
        self._delete(project, code=code)
        return True

    def remove_owner(self, removed: RemovedUser) -> None:
        """Remove what the Runtime keeps for a removed User, whose records are gone: their
        Projects, code and home included, then their Claude Account. Retried in the background
        until Docker lets it all go."""
        try:
            self._remove_owner(removed)
        except RuntimeUnavailable as error:
            log.warning("Cannot remove what Docker keeps for a removed User yet: %s", error)
            threading.Thread(
                target=self._remove_owner_until_done,
                args=(removed,),
                name=f"remove-{removed.runtime_id}",
                daemon=True,
            ).start()

    def resume(self) -> None:
        """Pick up what was in progress when Dockyard stopped: clones, and Projects waiting for
        a Claude Account (connected meanwhile, or with a sign-in in progress)."""
        with self._db.transaction() as conn:
            rows = conn.execute(f"SELECT {_COLUMNS}, owner FROM projects").fetchall()
            waiting = conn.execute(
                "SELECT DISTINCT owner FROM projects WHERE should_run = 1"
            ).fetchall()
            revoked = conn.execute(
                "SELECT DISTINCT owner FROM projects JOIN users ON runtime_id = owner"
                " WHERE privileged = 1 AND privileged_projects = 0"
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
                        cpus=row["cpus"],
                        memory=row["memory"],
                    ),
                )
        for (owner,) in revoked:  # stopped before, unless Dockyard stopped first
            self._stop_privileged_in_background(owner)
        for (owner,) in waiting:
            if self._runtime.login_environment_running(owner):
                self._follow_sign_in_in_background(owner)
            else:
                self._start_waiting(owner)

    def _delete(self, project: str, *, code: bool) -> None:
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
        """Start its Remote Control Server, or have it wait for its owner's Claude Account. It
        is already active: never marked so here, which would undo a stop made meanwhile. A
        Privileged Project whose owner no longer holds the permission is stopped instead."""
        if self._runtime.claude_account_connected(row["owner"]):
            with suppress(ProjectMissing):  # its status says so
                with self._starting_privileged if row["privileged"] else nullcontext():
                    if row["privileged"] and not self._permitted(row["owner"]):
                        self._should_run(row["id"], False)
                        return
                    self._runtime.start_project(
                        row["owner"],
                        row["id"],
                        name=row["name"],
                        permission_mode=row["permission_mode"],
                        # Within the Instance limits as they are now, even if lowered since.
                        cpus=min(row["cpus"], self.limits.max_cpus),
                        memory=min(row["memory"], self.limits.max_memory),
                        privileged=bool(row["privileged"]),
                    )
                if row["git_credential"]:
                    start = self._runtime.project_starts().get(row["id"])
                    self._write_git_credential(row, start)

    def _stats(self, project: str) -> Stats:
        """Raises ProjectRefused, RuntimeUnavailable."""
        stats = self._runtime.project_stats(project)
        if stats is None:
            raise ProjectRefused(Refusal.NOT_RUNNING)
        return stats

    def _stop(self, project: str) -> None:
        """Stop its Remote Control Server, or its wait: it is no longer active."""
        self._should_run(project, False)
        self._runtime.stop_project(project)

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
            # Not if its User stopped it meanwhile: it is no longer active.
            if (row := self._row_by_id(project)) and row["should_run"]:
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

    def _remove_owner(self, removed: RemovedUser) -> None:
        for project in removed.projects:
            self._runtime.remove_project(project, code=True)
            with self._lock:
                self._clones.pop(project, None)
                self._written.pop(project, None)
        self._runtime.remove_claude_account(removed.runtime_id)

    def _stop_privileged(self, owner: str) -> None:
        """Stop the owner's Privileged Projects, unless the permission was granted again
        meanwhile. Raises RuntimeUnavailable."""
        with self._starting_privileged:
            if self._permitted(owner):
                return
            with self._db.transaction() as conn:
                rows = conn.execute(
                    "SELECT id FROM projects WHERE owner = ? AND privileged = 1", (owner,)
                ).fetchall()
            for row in rows:
                self._stop(row["id"])

    def _stop_privileged_in_background(self, owner: str) -> None:
        threading.Thread(
            target=self._stop_privileged_until_done,
            args=(owner,),
            name=f"revoke-{owner}",
            daemon=True,
        ).start()

    def _stop_privileged_until_done(self, owner: str) -> None:
        while True:
            try:
                self._stop_privileged(owner)
                return
            except RuntimeUnavailable as error:
                log.warning("Cannot stop the Privileged Projects of %s yet: %s", owner, error)
            if self._closed.wait(self._poll_interval):
                return

    def _remove_owner_until_done(self, removed: RemovedUser) -> None:
        while not self._closed.wait(self._poll_interval):
            try:
                self._remove_owner(removed)
                return
            except RuntimeUnavailable as error:
                log.warning("Cannot remove what Docker keeps for a removed User yet: %s", error)

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

    def _permitted(self, owner: str) -> bool:
        """Whether the owner holds the permission to create Privileged Projects."""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT privileged_projects FROM users WHERE runtime_id = ?", (owner,)
            ).fetchone()
        return bool(row and row["privileged_projects"])

    def _should_run(self, project: str, should_run: bool) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE projects SET should_run = ? WHERE id = ?", (should_run, project))

    def _forget(self, project: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM projects WHERE id = ?", (project,))

    def _owned(self, row: sqlite3.Row) -> OwnedProject:
        return OwnedProject(row["owner_email"], self._project(row))

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
            cpus=row["cpus"],
            memory=row["memory"],
            privileged=bool(row["privileged"]),
        )


_COLUMNS = (
    "id, name, repository, provider, visibility, permission_mode, should_run, git_credential,"
    " cpus, memory, privileged"
)
# Projects with their owner's email, as Administrators see them.
_OWNED = (
    f"SELECT {_COLUMNS}, (SELECT email FROM users WHERE runtime_id = owner) AS owner_email"
    " FROM projects"
)
# How many active Projects an owner (the parameter) has: those that should run, whether they
# run, wait for a Claude Account, keep failing or are still being cloned.
_ACTIVE = "SELECT count(*) FROM projects WHERE owner = ? AND should_run = 1"


def _readable(output: str) -> str:
    """git's output as a terminal would leave it: each progress line shows its last update."""
    lines = [line.rstrip("\r").rsplit("\r", 1)[-1] for line in output.rstrip("\n").split("\n")]
    return "\n".join(lines[-CLONE_OUTPUT_LINES:])
