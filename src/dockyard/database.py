"""SQLite storage for what Dockyard owns. Container state is always read from Docker, never here."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

# Append only: each entry runs once, in order, and its position is the schema version.
MIGRATIONS = (
    """
    CREATE TABLE users (
        id         INTEGER PRIMARY KEY,
        email      TEXT NOT NULL UNIQUE,
        role       TEXT NOT NULL CHECK (role IN ('administrator', 'user')),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    );
    """,
    # runtime_id names the User's resources in the Runtime (their Claude config). It is random, so
    # it is never reused: not after a User is removed, not by a reinstalled Instance.
    """
    CREATE TABLE users_new (
        id         INTEGER PRIMARY KEY,
        email      TEXT NOT NULL UNIQUE,
        role       TEXT NOT NULL CHECK (role IN ('administrator', 'user')),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        runtime_id TEXT NOT NULL UNIQUE DEFAULT (lower(hex(randomblob(16))))
    );
    INSERT INTO users_new (id, email, role, created_at)
        SELECT id, email, role, created_at FROM users;
    DROP TABLE users;
    ALTER TABLE users_new RENAME TO users;
    """,
    # id names the Project in the Runtime and its path, /workspace/<id>: random, never reused.
    """
    CREATE TABLE projects (
        id         TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
        owner      TEXT NOT NULL REFERENCES users (runtime_id),
        name       TEXT NOT NULL,
        repository TEXT NOT NULL,
        provider   TEXT NOT NULL CHECK (provider IN ('github', 'gitlab')),
        visibility TEXT NOT NULL CHECK (visibility IN ('public', 'private')),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    );
    CREATE INDEX projects_owner ON projects (owner);
    """,
    # should_run: the User wants its server running. Whether it runs is read from the Runtime;
    # a Project that should run but has no environment waits for its owner's Claude Account.
    """
    ALTER TABLE projects ADD COLUMN permission_mode TEXT NOT NULL DEFAULT 'default'
        CHECK (permission_mode IN ('default', 'acceptEdits', 'auto', 'bypassPermissions'));
    ALTER TABLE projects ADD COLUMN should_run INTEGER NOT NULL DEFAULT 0
        CHECK (should_run IN (0, 1));
    """,
    # token: encrypted with the Instance secret key, never stored in clear.
    """
    CREATE TABLE git_credentials (
        id         TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
        owner      TEXT NOT NULL REFERENCES users (runtime_id),
        label      TEXT NOT NULL,
        provider   TEXT NOT NULL CHECK (provider IN ('github', 'gitlab')),
        token      BLOB NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    );
    CREATE INDEX git_credentials_owner ON git_credentials (owner);
    """,
    # git_credential: the Git Credential the Project clones with and hands to Claude; required
    # for a private Project. A Git Credential cannot be deleted while a Project uses it.
    """
    ALTER TABLE projects ADD COLUMN git_credential TEXT REFERENCES git_credentials (id);
    CREATE INDEX projects_git_credential ON projects (git_credential);
    """,
    # An invited email becomes a User, and loses its Invitation, on its first access.
    """
    CREATE TABLE invitations (
        email      TEXT PRIMARY KEY,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    );
    """,
    # What the Project may use, chosen at creation within the Instance limits: CPUs, and RAM
    # in bytes. Projects created before get what every Project got until then.
    """
    ALTER TABLE projects ADD COLUMN cpus REAL NOT NULL DEFAULT 2 CHECK (cpus > 0);
    ALTER TABLE projects ADD COLUMN memory INTEGER NOT NULL DEFAULT 4294967296
        CHECK (memory > 0);
    """,
    # privileged_projects: an Administrator granted the User the permission to create Privileged
    # Projects, whose environment runs privileged (root on the host) while the permission holds.
    """
    ALTER TABLE users ADD COLUMN privileged_projects INTEGER NOT NULL DEFAULT 0
        CHECK (privileged_projects IN (0, 1));
    ALTER TABLE projects ADD COLUMN privileged INTEGER NOT NULL DEFAULT 0
        CHECK (privileged IN (0, 1));
    """,
    # Images an Administrator defines on top of the Base Image. log: what the build printed,
    # recorded once it ended. A Project's image is NULL for the Base Image; an Image cannot be
    # deleted while a Project uses it.
    """
    CREATE TABLE images (
        id           TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
        label        TEXT NOT NULL,
        description  TEXT NOT NULL,
        instructions TEXT NOT NULL,
        status       TEXT NOT NULL DEFAULT 'building'
            CHECK (status IN ('building', 'ready', 'failed')),
        failure      TEXT CHECK (failure IN ('build', 'claude_missing', 'root_user')),
        log          TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    );
    ALTER TABLE projects ADD COLUMN image TEXT REFERENCES images (id);
    CREATE INDEX projects_image ON projects (image);
    """,
    # build: the build of the Image that Projects start from, NULL until one is ready; each
    # build has its own name, and those made before were named after their Image. base_build:
    # the Base Image its last build started from, to build it again once the Base Image changes.
    # retired_builds: builds no longer started from, removed once no Project runs them.
    """
    ALTER TABLE images ADD COLUMN build TEXT;
    ALTER TABLE images ADD COLUMN base_build TEXT;
    UPDATE images SET build = id WHERE status = 'ready';
    CREATE TABLE retired_builds (build TEXT PRIMARY KEY);
    """,
    # Local sign-in. password: the scrypt hash of the User's password, NULL until they set one.
    # A session is known by the hash of its token alone, and ends with its User.
    """
    ALTER TABLE users ADD COLUMN password TEXT;
    CREATE TABLE sessions (
        token_hash   TEXT PRIMARY KEY,
        email        TEXT NOT NULL REFERENCES users (email) ON DELETE CASCADE,
        created_at   TEXT NOT NULL,
        last_used_at TEXT NOT NULL
    );
    CREATE INDEX sessions_email ON sessions (email);
    """,
)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn

    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
                conn.executescript(f"BEGIN; {script}; PRAGMA user_version = {number}; COMMIT;")
