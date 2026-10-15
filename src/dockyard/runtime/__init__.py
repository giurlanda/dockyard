"""Runtime: the only part of Dockyard that knows about Docker.

The rest of Dockyard uses the port below. `docker.DockerRuntime` implements it on Docker and
`fake.FakeRuntime` in memory, for tests; the Runtime contract suite (tests/runtime) holds both to
the same behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

LOGIN_TIMEOUT = 15 * 60  # seconds a login environment waits for its User before it ends
CLONE_TIMEOUT = 30 * 60  # seconds a clone may take before it is given up


def project_path(project: str) -> str:
    """Where a Project's code lives in its environment. Claude Code keys trust, Remote Control
    and transcripts by this path, so each Project has its own."""
    return f"/workspace/{project}"


class RuntimeUnavailable(Exception):
    """The Runtime cannot do this right now, for example because Docker is unreachable."""


class LoginNotRunning(Exception):
    """The User has no login environment running."""


class CloneState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class Clone:
    state: CloneState
    output: str  # what git printed so far, progress included


class ProjectStatus(StrEnum):
    """What the Runtime holds for a Project."""

    CLONING = "cloning"  # its clone has not been finished with yet
    STOPPED = "stopped"  # its code is there, nothing runs
    MISSING = "missing"  # nothing is there


class Tty(Protocol):
    """A program's TTY: what it prints, and keys and a size from whoever is watching it."""

    def read(self) -> bytes:
        """What the program printed next, waiting for it; b"" once the TTY has ended."""
        ...

    def write(self, data: bytes) -> None: ...

    def resize(self, rows: int, cols: int) -> None: ...

    def close(self) -> None:
        """Stop watching. The program keeps running."""
        ...


class Runtime(Protocol):
    """Users are named by their runtime id: random, never reused, never an email."""

    def check_reachable(self) -> None:
        """Raises RuntimeUnavailable."""
        ...

    def claude_account_connected(self, user: str) -> bool:
        """Whether the User's Claude config holds credentials."""
        ...

    def start_login_environment(self, user: str) -> None:
        """Start a temporary login environment for the User, replacing any they already have.

        It runs `claude auth login` from the Base Image with the User's own Claude config, and
        ends when the login does, when it is stopped, or after LOGIN_TIMEOUT.
        """
        ...

    def login_environment_running(self, user: str) -> bool: ...

    def attach_login_environment(self, user: str) -> Tty:
        """The TTY of the User's login environment, starting with the lines it already printed.

        Raises LoginNotRunning.
        """
        ...

    def stop_login_environment(self, user: str) -> None: ...

    def start_clone(self, user: str, project: str, repository: str) -> None:
        """Give the Project its own code volume, owned by the non-root user, and start cloning
        the public `repository` into project_path(project), as the non-root user.

        The clone stays (see `clone`) until `finish_clone` or `remove_project`; it is given up
        after CLONE_TIMEOUT.
        """
        ...

    def clone(self, project: str) -> Clone | None:
        """The Project's clone, or None once it has been finished with (or never started)."""
        ...

    def finish_clone(self, project: str) -> None:
        """Let go of a clone that ended, keeping the code it cloned."""
        ...

    def project_status(self, project: str) -> ProjectStatus: ...

    def remove_project(self, project: str, *, code: bool) -> None:
        """Remove everything the Project runs (its clone included), and its code only if asked."""
        ...
