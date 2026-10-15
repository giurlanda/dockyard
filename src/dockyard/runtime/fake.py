"""In-memory Runtime, for tests. The Runtime contract suite keeps it honest against Docker."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import (
    LOGIN_TIMEOUT,
    Clone,
    CloneState,
    LoginNotRunning,
    ProjectStatus,
    RuntimeUnavailable,
    Tty,
    project_path,
)


class FakeRuntime:
    def __init__(self, *, login_timeout: float = LOGIN_TIMEOUT) -> None:
        self._lock = threading.Lock()
        self._signed_in: set[str] = set()  # Users whose Claude config holds credentials
        self._logins: dict[str, _Login] = {}
        self._login_timeout = login_timeout
        self._reachable = True
        self._repositories: set[str] = set()  # the public repositories that exist
        self._projects: dict[str, _Project] = {}
        self._clones_paused = False

    def publish_repository(self, url: str) -> None:
        """From now on, `url` is a public repository that can be cloned."""
        with self._lock:
            self._repositories.add(url)

    @contextmanager
    def clones_paused(self) -> Iterator[None]:
        """Clones started meanwhile keep running until the end of the block, then end."""
        with self._lock:
            self._clones_paused = True
        try:
            yield
        finally:
            with self._lock:
                self._clones_paused = False
                for project in self._projects.values():
                    if project.clone and project.clone.state == CloneState.RUNNING:
                        project.clone = project.clone_outcome

    @contextmanager
    def unavailable(self) -> Iterator[None]:
        """Every operation fails meanwhile, as when Docker is unreachable."""
        self._reachable = False
        try:
            yield
        finally:
            self._reachable = True

    def check_reachable(self) -> None:
        if not self._reachable:
            raise RuntimeUnavailable("The fake Runtime is unavailable.")

    def claude_account_connected(self, user: str) -> bool:
        self.check_reachable()
        with self._lock:
            return user in self._signed_in

    def start_login_environment(self, user: str) -> None:
        self.check_reachable()
        login = _Login(
            signed_in=lambda: self._sign_in(user), ended=lambda: self._forget(user, login)
        )
        with self._lock:
            previous = self._logins.get(user)
            self._logins[user] = login
        if previous:
            previous.end()
        timer = threading.Timer(self._login_timeout, login.end)
        timer.daemon = True
        timer.start()

    def login_environment_running(self, user: str) -> bool:
        self.check_reachable()
        with self._lock:
            return user in self._logins

    def attach_login_environment(self, user: str) -> Tty:
        self.check_reachable()
        with self._lock:
            login = self._logins.get(user)
        if login is None:
            raise LoginNotRunning
        return login.attach()

    def stop_login_environment(self, user: str) -> None:
        self.check_reachable()
        with self._lock:
            login = self._logins.get(user)
        if login:
            login.end()

    def start_clone(self, user: str, project: str, repository: str) -> None:
        self.check_reachable()
        # What git prints when it clones, as the Runtime contract suite sees it.
        started = f"Cloning into '{project_path(project)}'...\n"
        if repository in self._repositories:
            outcome = Clone(
                CloneState.SUCCEEDED,
                started + "remote: Enumerating objects: 3, done.\n"
                "Receiving objects: 100% (3/3), done.\n",
            )
        else:
            host = urlsplit(repository).hostname
            outcome = Clone(
                CloneState.FAILED,
                started + f"fatal: could not read Username for 'https://{host}': "
                "terminal prompts disabled\n",
            )
        with self._lock:
            running = Clone(CloneState.RUNNING, started)
            self._projects[project] = _Project(
                code=True,
                clone=running if self._clones_paused else outcome,
                clone_outcome=outcome,
            )

    def clone(self, project: str) -> Clone | None:
        self.check_reachable()
        with self._lock:
            found = self._projects.get(project)
            return found.clone if found else None

    def finish_clone(self, project: str) -> None:
        self.check_reachable()
        with self._lock:
            if found := self._projects.get(project):
                found.clone = None

    def project_status(self, project: str) -> ProjectStatus:
        self.check_reachable()
        with self._lock:
            found = self._projects.get(project)
            if found and found.clone:
                return ProjectStatus.CLONING
            return ProjectStatus.STOPPED if found and found.code else ProjectStatus.MISSING

    def remove_project(self, project: str, *, code: bool) -> None:
        self.check_reachable()
        with self._lock:
            if found := self._projects.get(project):
                found.clone = None
                found.code = found.code and not code

    def _sign_in(self, user: str) -> None:
        with self._lock:
            self._signed_in.add(user)

    def _forget(self, user: str, login: _Login) -> None:
        with self._lock:
            if self._logins.get(user) is login:
                del self._logins[user]


@dataclass
class _Project:
    code: bool  # its code volume exists
    clone: Clone | None
    clone_outcome: Clone = field(repr=False)  # how its clone ends


class _Login:
    """`claude auth login` as the contract suite's stand-in plays it, on a TTY of its own."""

    def __init__(self, *, signed_in: Callable[[], None], ended: Callable[[], None]) -> None:
        self._lock = threading.RLock()
        self._signed_in = signed_in
        self._ended = ended
        self._printed = b""
        self._watchers: list[_Watcher] = []
        self._line = ""
        self._size = (24, 80)
        self._over = False
        self._print(
            "Opening browser to sign in…\r\n"
            "If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?fake=1\r\n"
            "Paste code here if prompted > "
        )

    def attach(self) -> _Watcher:
        with self._lock:
            if self._over:
                raise LoginNotRunning
            watcher = _Watcher(self)
            # Like Docker's logs, the replay holds complete lines only: never a pending prompt.
            watcher.show(self._printed[: self._printed.rfind(b"\n") + 1])
            self._watchers.append(watcher)
            return watcher

    def detach(self, watcher: _Watcher) -> None:
        with self._lock:
            if watcher in self._watchers:
                self._watchers.remove(watcher)

    def type(self, keys: str) -> None:
        with self._lock:
            for key in keys:
                if self._over:
                    return
                if key in "\r\n":
                    self._print("\r\n")
                    self._submit(self._line)
                else:
                    self._line += key
                    self._print(key)  # the terminal echoes what is typed

    def resize(self, rows: int, cols: int) -> None:
        with self._lock:
            self._size = (rows, cols)

    def _submit(self, code: str) -> None:
        rows, cols = self._size
        self._print(f"Terminal size: {rows} {cols}\r\n")
        if code:
            self._signed_in()
            self._print("Login successful.\r\n")
        else:
            self._print("Login failed: no code.\r\n")
        self.end()

    def end(self) -> None:
        with self._lock:
            if self._over:
                return
            self._over = True
            for watcher in self._watchers:
                watcher.show(b"")
            self._watchers.clear()
        self._ended()

    def _print(self, text: str) -> None:
        data = text.encode()
        self._printed += data
        for watcher in self._watchers:
            watcher.show(data)


class _Watcher:
    """A Tty on a fake login."""

    def __init__(self, login: _Login) -> None:
        self._login = login
        self._output: queue.SimpleQueue[bytes] = queue.SimpleQueue()

    def show(self, data: bytes) -> None:
        self._output.put(data)

    def read(self) -> bytes:
        data = self._output.get()
        if not data:
            self._output.put(data)  # ended: every later read says so too
        return data

    def write(self, data: bytes) -> None:
        self._login.type(data.decode(errors="replace"))

    def resize(self, rows: int, cols: int) -> None:
        self._login.resize(rows, cols)

    def close(self) -> None:
        self._login.detach(self)
        self.show(b"")
