"""Projects: which Projects a User may create, the status shown to them, and their lifecycle."""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import StrEnum
from urllib.parse import urlsplit

from .accounts import User
from .database import Database
from .runtime import Clone, CloneState, ProjectStatus, Runtime, RuntimeUnavailable

log = logging.getLogger(__name__)

NAME_MAX_LENGTH = 60
CLONE_POLL_INTERVAL = 1.0  # seconds between two looks at a clone in progress
CLONE_OUTPUT_LINES = 40  # the end of what git printed: its progress, or why it failed


class Provider(StrEnum):
    GITHUB = "github"
    GITLAB = "gitlab"


PROVIDERS = {"github.com": Provider.GITHUB, "gitlab.com": Provider.GITLAB}
# A path segment of a repository: an owner, a group or the repository itself (never "." or "..").
SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


class Visibility(StrEnum):
    PUBLIC = "public"


class Status(StrEnum):
    """The status a User sees, worked out from what the Runtime holds."""

    CLONING = "cloning"
    STOPPED = "stopped"
    ERROR = "error"


_STATUS = {
    ProjectStatus.CLONING: Status.CLONING,
    ProjectStatus.STOPPED: Status.STOPPED,
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


class ProjectRefused(Exception):
    def __init__(self, reason: Refusal) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Repository:
    url: str
    provider: Provider


def parse_repository(text: str) -> Repository:
    """A public repository on github.com or gitlab.com, by its plain https URL.

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
    status: Status


@dataclass(frozen=True)
class CloneReport:
    """How a Project's clone went. A failed one outlives its Project, so its User can see why."""

    owner: str  # the owner's runtime id
    name: str
    repository: str
    state: CloneState
    output: str


class Projects:
    def __init__(
        self, database: Database, runtime: Runtime, *, poll_interval: float = CLONE_POLL_INTERVAL
    ) -> None:
        self._db = database
        self._runtime = runtime
        self._poll_interval = poll_interval
        self._lock = threading.Lock()
        self._clones: dict[str, CloneReport] = {}  # since Dockyard started
        self._deleting: set[str] = set()

    def create(self, owner: User, *, name: str, repository: str, visibility: Visibility) -> Project:
        """Record the Project and start cloning it. Raises ProjectRefused, RuntimeUnavailable."""
        name = valid_name(name)
        source = parse_repository(repository)
        with self._db.transaction() as conn:
            (project,) = conn.execute(
                "INSERT INTO projects (owner, name, repository, provider, visibility)"
                " VALUES (?, ?, ?, ?, ?) RETURNING id",
                (owner.runtime_id, name, source.url, source.provider, visibility),
            ).fetchone()
        try:
            self._runtime.start_clone(owner.runtime_id, project, source.url)
        except RuntimeUnavailable:
            with suppress(RuntimeUnavailable):  # leave nothing behind, as far as Docker lets us
                self._runtime.remove_project(project, code=True)
            self._forget(project)
            raise
        self._follow(project, owner=owner.runtime_id, name=name, repository=source.url)
        return Project(project, name, source.url, source.provider, visibility, Status.CLONING)

    def list(self, owner: User) -> list[Project]:
        with self._db.transaction() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM projects WHERE owner = ? ORDER BY created_at, rowid",
                (owner.runtime_id,),
            ).fetchall()
        return [self._project(row) for row in rows]

    def get(self, owner: User, project: str) -> Project | None:
        with self._db.transaction() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM projects WHERE owner = ? AND id = ?",
                (owner.runtime_id, project),
            ).fetchone()
        return self._project(row) if row else None

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
        finally:
            with self._lock:
                self._deleting.discard(project)
        return True

    def resume_clones(self) -> None:
        """Follow again the clones that were in progress when Dockyard stopped."""
        with self._db.transaction() as conn:
            rows = conn.execute("SELECT id, owner, name, repository FROM projects").fetchall()
        for row in rows:
            if self._runtime.project_status(row["id"]) == ProjectStatus.CLONING:
                self._follow(
                    row["id"], owner=row["owner"], name=row["name"], repository=row["repository"]
                )

    def _follow(self, project: str, *, owner: str, name: str, repository: str) -> None:
        with self._lock:
            self._clones[project] = CloneReport(owner, name, repository, CloneState.RUNNING, "")
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

    def _forget(self, project: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM projects WHERE id = ?", (project,))

    def _project(self, row: sqlite3.Row) -> Project:
        return Project(
            id=row["id"],
            name=row["name"],
            repository=row["repository"],
            provider=Provider(row["provider"]),
            visibility=Visibility(row["visibility"]),
            status=_STATUS[self._runtime.project_status(row["id"])],
        )


_COLUMNS = "id, name, repository, provider, visibility"


def _readable(output: str) -> str:
    """git's output as a terminal would leave it: each progress line shows its last update."""
    lines = [line.rstrip("\r").rsplit("\r", 1)[-1] for line in output.rstrip("\n").split("\n")]
    return "\n".join(lines[-CLONE_OUTPUT_LINES:])
