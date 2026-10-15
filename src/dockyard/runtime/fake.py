"""In-memory Runtime, for tests. The Runtime contract suite keeps it honest against Docker."""

from __future__ import annotations

import queue
import secrets
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import (
    DEFAULT_DNS,
    LOGIN_TIMEOUT,
    PROJECT_CPUS,
    PROJECT_MEMORY,
    PROJECT_PIDS_LIMIT,
    TRUST_PROMPT,
    Clone,
    CloneState,
    LoginNotRunning,
    Output,
    ProjectMissing,
    ProjectNotRunning,
    ProjectStatus,
    RuntimeUnavailable,
    Stats,
    Tty,
    project_path,
)

# The last commit of every repository the fake publishes, as on github.com/octocat/Hello-World.
LAST_COMMIT = "Merge pull request #6 from Spaceghost/patch-1"
# What a running Project uses, until a test says otherwise (see `use`).
USED_CPU = 0.5  # percent of one CPU
USED_MEMORY = 200 * 1024**2  # bytes
CTRL_C = "\x03"


class FakeRuntime:
    def __init__(
        self, *, dns: Sequence[str] = DEFAULT_DNS, login_timeout: float = LOGIN_TIMEOUT
    ) -> None:
        self._lock = threading.Lock()
        self._dns = tuple(dns)
        self._signed_in: set[str] = set()  # Users whose Claude config holds credentials
        self._trusted: dict[str, set[str]] = {}  # the paths each User's Claude config trusts
        # Each User's Remote Control environments, by path, as Claude Code keeps them.
        self._remote_control: dict[str, dict[str, str]] = {}
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

    def ensure_infrastructure(self) -> None:
        self.check_reachable()

    def crash(self, project: str) -> None:
        """The Project's Remote Control Server keeps failing from now on, until started again."""
        with self._lock:
            self._projects[project].crashed = True

    def lose_trust(self, project: str) -> None:
        """Another writer of the User's Claude config drops the trust of the Project's path, and
        its Remote Control Server restarts (as after a crash): it asks for that trust again."""
        with self._lock:
            found = self._projects[project]
            self._trusted.get(found.user, set()).discard(project_path(project))
            server = found.server
        if server:
            server.exit()

    def use(self, project: str, *, cpu: float, memory: int) -> None:
        """The Project uses this much from now on."""
        with self._lock:
            self._projects[project].usage = (cpu, memory)

    def watching(self, project: str) -> int:
        """How many follow the Project's server: its log and its console."""
        with self._lock:
            server = self._projects[project].server
        return server.watching() if server else 0

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
                user=user,
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

    def start_project(self, user: str, project: str, *, name: str, permission_mode: str) -> None:
        self.check_reachable()
        with self._lock:
            found = self._projects.get(project)
            if not found or not found.code:
                raise ProjectMissing(project)
            self._trusted.setdefault(user, set()).add(project_path(project))
            found.command = (
                f"remote-control --name={name} --spawn worktree --permission-mode {permission_mode}"
            )
            previous, found.server, found.crashed = found.server, None, False
        if previous:
            previous.end()
        self._run(project)

    def stop_project(self, project: str) -> None:
        self.check_reachable()
        with self._lock:
            found = self._projects.get(project)
            server = found.server if found else None
            if found:
                found.server = None
        if server:
            server.end()

    def project_log(self, project: str) -> str:
        self.check_reachable()
        with self._lock:
            found = self._projects.get(project)
            server = found.server if found else None
        return server.printed.decode(errors="replace") if server else ""

    def follow_project_log(self, project: str) -> Output:
        self.check_reachable()
        with self._lock:
            found = self._projects.get(project)
            server = found.server if found else None
        if server is None:
            raise ProjectNotRunning(project)
        return server.attach()

    def attach_project(self, project: str) -> Tty:
        self.check_reachable()
        with self._lock:
            found = self._projects.get(project)
            server = found.server if found and not found.crashed else None
        if server is None:
            raise ProjectNotRunning(project)
        return server.attach()

    def project_stats(self, project: str) -> Stats | None:
        self.check_reachable()
        with self._lock:
            found = self._projects.get(project)
            if not found or not found.server or found.crashed:
                return None
            cpu, memory = found.usage
            return Stats(cpu=cpu, cpus=PROJECT_CPUS, memory=memory, memory_limit=PROJECT_MEMORY)

    def project_status(self, project: str) -> ProjectStatus:
        self.check_reachable()
        with self._lock:
            found = self._projects.get(project)
            if found and found.clone:
                return ProjectStatus.CLONING
            if not found or not found.code:
                return ProjectStatus.MISSING
            if found.server is None:
                return ProjectStatus.STOPPED
            if found.crashed:
                return ProjectStatus.FAILED
            server = found.server
        return ProjectStatus.TRUST_PROMPT if server.asking else ProjectStatus.RUNNING

    def remove_project(self, project: str, *, code: bool) -> None:
        self.check_reachable()
        with self._lock:
            found = self._projects.get(project)
            server = found.server if found else None
            if found:
                found.clone = None
                found.server = None
                if code:
                    found.code = False
                    found.starts = 0  # its home goes with its code
        if server:
            server.end()

    def _run(self, project: str) -> None:
        """Start the Project's Remote Control Server in its environment, as the stand-in plays it.

        Never called holding self._lock: the server calls back into the Runtime.
        """
        with self._lock:
            found = self._projects[project]
            path = project_path(project)
            trusted = path in self._trusted.get(found.user, set())
            command = found.command
        server = _Server(
            [command, f"in {path}"],
            trusted=trusted,
            trust=lambda: self._trust(found.user, path),
            report=lambda: self._report(project),
            exited=lambda: self._restart(project, server),
        )
        with self._lock:
            found.server = server

    def _trust(self, user: str, path: str) -> None:
        with self._lock:
            self._trusted.setdefault(user, set()).add(path)

    def _report(self, project: str) -> list[str]:
        """What the contract suite's stand-in reports once its directory is trusted."""
        with self._lock:
            found = self._projects[project]
            path = project_path(project)
            environment = self._remote_control.setdefault(found.user, {}).setdefault(
                path, secrets.token_hex(8)
            )
            found.starts += 1
            return [
                f"trusted: {' '.join(sorted(self._trusted.get(found.user, set())))}",
                "claude config mode: 600",
                f"environment: {environment}",
                f"starts: {found.starts}",
                f"code: {LAST_COMMIT}",
                "user: 1000",
                "capabilities: 0000000000000000",
                "no new privileges: 1",
                f"pids limit: {PROJECT_PIDS_LIMIT}",
                f"memory limit: {PROJECT_MEMORY}",
                f"cpu limit: {PROJECT_CPUS * 100000} 100000",
                f"dns: {' '.join(self._dns)}",
                "anthropic environment: ",
            ]

    def _restart(self, project: str, server: _Server) -> None:
        """The server exited by itself: Docker starts it again, in the same environment."""
        with self._lock:
            found = self._projects.get(project)
            if not found or found.server is not server:  # stopped meanwhile
                return
        self._run(project)

    def _sign_in(self, user: str) -> None:
        with self._lock:
            self._signed_in.add(user)

    def _forget(self, user: str, login: _Login) -> None:
        with self._lock:
            if self._logins.get(user) is login:
                del self._logins[user]


@dataclass
class _Project:
    user: str
    code: bool  # its code volume exists
    clone: Clone | None
    clone_outcome: Clone = field(repr=False)  # how its clone ends
    command: str = ""  # its server's arguments
    server: _Server | None = None  # while its environment is there
    crashed: bool = False  # its server keeps failing
    starts: int = 0  # how many times its server started, as counted in its home
    usage: tuple[float, int] = (USED_CPU, USED_MEMORY)


class _Program:
    """A program on a TTY of its own, as the contract suite's stand-in plays it. The TTY echoes
    what is typed; Ctrl+C interrupts the program."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.printed = b""
        self._watchers: list[_Watcher] = []
        self._line = ""
        self._size = (24, 80)
        self._over = False

    def attach(self) -> _Watcher:
        with self._lock:
            watcher = _Watcher(self)
            # Like Docker's logs, the replay holds complete lines only: never a pending prompt.
            watcher.show(self.printed[: self.printed.rfind(b"\n") + 1])
            if self._over:
                watcher.show(b"")
            else:
                self._watchers.append(watcher)
            return watcher

    def detach(self, watcher: _Watcher) -> None:
        with self._lock:
            if watcher in self._watchers:
                self._watchers.remove(watcher)

    def watching(self) -> int:
        with self._lock:
            return len(self._watchers)

    def type(self, keys: str) -> None:
        with self._lock:
            for key in keys:
                if self._over:
                    return
                if key == CTRL_C:
                    self._print("^C\r\n")
                    self.exit()
                elif key in "\r\n":
                    self._print("\r\n")
                    line, self._line = self._line, ""
                    self._submit(line)
                else:
                    self._line += key
                    self._print(key)

    def resize(self, rows: int, cols: int) -> None:
        with self._lock:
            self._size = (rows, cols)
            self._resized()

    def end(self) -> None:
        """It is stopped from outside."""
        with self._lock:
            if self._over:
                return
            self._over = True
            for watcher in self._watchers:
                watcher.show(b"")
            self._watchers.clear()

    def exit(self) -> None:
        """It ends by itself."""
        self.end()

    def _submit(self, line: str) -> None:
        """A line was typed."""

    def _resized(self) -> None:
        """Its terminal changed size."""

    def _print(self, text: str) -> None:
        data = text.encode()
        self.printed += data
        for watcher in self._watchers:
            watcher.show(data)


class _Login(_Program):
    """`claude auth login`."""

    def __init__(self, *, signed_in: Callable[[], None], ended: Callable[[], None]) -> None:
        super().__init__()
        self._signed_in = signed_in
        self._ended = ended
        self._print(
            "Opening browser to sign in…\r\n"
            "If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?fake=1\r\n"
            "Paste code here if prompted > "
        )

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
            over = self._over
            super().end()
        if not over:
            self._ended()


class _Server(_Program):
    """`claude remote-control ...`: it asks to trust its directory unless the Claude config
    already does, reports what it sees, then waits, reporting its terminal size when it changes.
    """

    def __init__(
        self,
        intro: list[str],
        *,
        trusted: bool,
        trust: Callable[[], None],
        report: Callable[[], list[str]],
        exited: Callable[[], None],
    ) -> None:
        super().__init__()
        self._trust = trust
        self._report = report
        self._exited = exited
        self.asking = not trusted
        self._lines(intro)
        if self.asking:
            self._lines(
                [
                    f"{TRUST_PROMPT}: is this a project you created or one you trust?",
                    "> 1. Yes, I trust this folder",
                    "  2. No, exit",
                ]
            )
        else:
            self._ready()

    def exit(self) -> None:
        with self._lock:
            over = self._over
            self.end()
        if not over:
            self._exited()

    def _submit(self, line: str) -> None:
        if not self.asking:
            return
        if line.strip() not in ("", "1"):
            self.exit()
            return
        self.asking = False
        self._trust()
        self._ready()

    def _ready(self) -> None:
        self._lines([*self._report(), "Remote Control ready"])

    def _resized(self) -> None:
        rows, cols = self._size
        self._lines([f"Terminal size: {rows} {cols}"])

    def _lines(self, lines: list[str]) -> None:
        self._print("".join(f"{line}\r\n" for line in lines))


class _Watcher:
    """A Tty on a fake program."""

    def __init__(self, program: _Program) -> None:
        self._program = program
        self._output: queue.SimpleQueue[bytes] = queue.SimpleQueue()

    def show(self, data: bytes) -> None:
        self._output.put(data)

    def read(self) -> bytes:
        data = self._output.get()
        if not data:
            self._output.put(data)  # ended: every later read says so too
        return data

    def write(self, data: bytes) -> None:
        self._program.type(data.decode(errors="replace"))

    def resize(self, rows: int, cols: int) -> None:
        self._program.resize(rows, cols)

    def close(self) -> None:
        self._program.detach(self)
        self.show(b"")
