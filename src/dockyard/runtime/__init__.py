"""Runtime: the only part of Dockyard that knows about Docker.

The rest of Dockyard uses the port below. `docker.DockerRuntime` implements it on Docker and
`fake.FakeRuntime` in memory, for tests; the Runtime contract suite (tests/runtime) holds both to
the same behaviour.
"""

from __future__ import annotations

from typing import Protocol

LOGIN_TIMEOUT = 15 * 60  # seconds a login environment waits for its User before it ends


class RuntimeUnavailable(Exception):
    """The Runtime cannot do this right now, for example because Docker is unreachable."""


class LoginNotRunning(Exception):
    """The User has no login environment running."""


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
