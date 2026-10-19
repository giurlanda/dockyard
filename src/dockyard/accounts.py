"""Accounts: Users, their role and Invitations. An email that is neither a User nor invited gets
nothing."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from .database import Database

EMAIL_MAX_LENGTH = 254
# One @, something on each side, a dot in the domain: what the authentication proxy verifies.
EMAIL = re.compile(r"^[^@\s/]+@[^@\s/]+\.[^@\s/]+$")


class Role(StrEnum):
    ADMINISTRATOR = "administrator"
    USER = "user"


class Refusal(StrEnum):
    EMAIL_INVALID = "email_invalid"
    ALREADY_USER = "already_user"
    ALREADY_INVITED = "already_invited"


class InvitationRefused(Exception):
    def __init__(self, reason: Refusal) -> None:
        self.reason = reason
        super().__init__(reason)


class LastAdministrator(Exception):
    """The Instance would be left without an Administrator."""


@dataclass(frozen=True)
class User:
    email: str
    role: Role
    runtime_id: str  # names the User's resources in the Runtime; never reused
    # An Administrator granted them the permission to create Privileged Projects.
    privileged_projects: bool = False


@dataclass(frozen=True)
class RemovedUser:
    runtime_id: str
    projects: list[str]  # the ids of the Projects removed with them


@dataclass(frozen=True)
class Invitation:
    email: str
    created_at: str


def normalise_email(email: str) -> str:
    return email.strip().lower()


class Accounts:
    def __init__(self, database: Database) -> None:
        self._db = database

    def bootstrap_first_administrator(self, email: str) -> None:
        """Bootstrap the first Administrator named in the Instance configuration."""
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO users (email, role) VALUES (?, ?) ON CONFLICT (email) DO NOTHING",
                (normalise_email(email), Role.ADMINISTRATOR),
            )

    def admit(self, email: str) -> User | None:
        """The User with this email, made a User now if it was invited; None if it was not."""
        if user := self.find_user(email):
            return user
        email = normalise_email(email)
        if not self._invited(email):
            return None  # no write for every request of a stranger
        with self._db.transaction() as conn:
            if _end_invitation(conn, email):
                conn.execute("INSERT INTO users (email, role) VALUES (?, ?)", (email, Role.USER))
        return self.find_user(email)

    def _invited(self, email: str) -> bool:
        with self._db.transaction() as conn:
            found = conn.execute("SELECT 1 FROM invitations WHERE email = ?", (email,))
            return found.fetchone() is not None

    def find_user(self, email: str) -> User | None:
        with self._db.transaction() as conn:
            row = conn.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE email = ?",
                (normalise_email(email),),
            ).fetchone()
        return _user(row) if row else None

    def users(self) -> list[User]:
        with self._db.transaction() as conn:
            rows = conn.execute(
                f"SELECT {_USER_COLUMNS} FROM users ORDER BY created_at, rowid"
            ).fetchall()
        return [_user(row) for row in rows]

    def change_role(self, email: str, role: Role) -> User | None:
        """None if there is no such User.

        Raises LastAdministrator.
        """
        email = normalise_email(email)
        with self._db.transaction() as conn:
            # One statement: two Administrators demoting each other cannot both succeed.
            row = conn.execute(
                "UPDATE users SET role = ? WHERE email = ?"
                f" AND (? = 'administrator' OR {_NOT_THE_LAST_ADMINISTRATOR})"
                f" RETURNING {_USER_COLUMNS}",
                (role, email, role),
            ).fetchone()
            if row is None and _exists(conn, email):
                raise LastAdministrator
        return _user(row) if row else None

    def permit_privileged_projects(self, email: str, permitted: bool) -> User | None:
        """Grant or revoke the User's permission to create Privileged Projects. None if there
        is no such User."""
        with self._db.transaction() as conn:
            row = conn.execute(
                "UPDATE users SET privileged_projects = ? WHERE email = ?"
                f" RETURNING {_USER_COLUMNS}",
                (permitted, normalise_email(email)),
            ).fetchone()
        return _user(row) if row else None

    def remove_user(self, email: str) -> RemovedUser | None:
        """Remove the User with what Dockyard records for them: their Projects and Git
        Credentials. What the Runtime keeps for them is left to the caller. None if there is no
        such User.

        Raises LastAdministrator.
        """
        email = normalise_email(email)
        with self._db.transaction() as conn:
            owner = "(SELECT runtime_id FROM users WHERE email = ?)"
            projects = conn.execute(
                f"DELETE FROM projects WHERE owner = {owner} RETURNING id", (email,)
            ).fetchall()
            conn.execute(f"DELETE FROM git_credentials WHERE owner = {owner}", (email,))
            row = conn.execute(
                f"DELETE FROM users WHERE email = ? AND {_NOT_THE_LAST_ADMINISTRATOR}"
                " RETURNING runtime_id",
                (email,),
            ).fetchone()
            if row is None:
                # Rolls back what was deleted above.
                if _exists(conn, email):
                    raise LastAdministrator
                return None
        return RemovedUser(row["runtime_id"], [project["id"] for project in projects])

    def invitations(self) -> list[Invitation]:
        """The Invitations nobody has used yet."""
        with self._db.transaction() as conn:
            rows = conn.execute(
                "SELECT email, created_at FROM invitations ORDER BY created_at, rowid"
            ).fetchall()
        return [Invitation(email=row["email"], created_at=row["created_at"]) for row in rows]

    def invite(self, email: str) -> Invitation:
        """Raises InvitationRefused."""
        email = valid_email(email)
        with self._db.transaction() as conn:
            if _exists(conn, email):
                raise InvitationRefused(Refusal.ALREADY_USER)
            row = conn.execute(
                "INSERT INTO invitations (email) VALUES (?) ON CONFLICT (email) DO NOTHING"
                " RETURNING email, created_at",
                (email,),
            ).fetchone()
        if row is None:
            raise InvitationRefused(Refusal.ALREADY_INVITED)
        return Invitation(email=row["email"], created_at=row["created_at"])

    def revoke_invitation(self, email: str) -> bool:
        """False if there is no such Invitation."""
        with self._db.transaction() as conn:
            return _end_invitation(conn, normalise_email(email))


def valid_email(email: str) -> str:
    email = normalise_email(email)
    if len(email) > EMAIL_MAX_LENGTH or not EMAIL.match(email):
        raise InvitationRefused(Refusal.EMAIL_INVALID)
    return email


_USER_COLUMNS = "email, role, runtime_id, privileged_projects"
# Holds for a User about to stop being an Administrator, unless they are the last one.
_NOT_THE_LAST_ADMINISTRATOR = (
    "(role = 'user' OR (SELECT count(*) FROM users WHERE role = 'administrator') > 1)"
)


def _end_invitation(conn: sqlite3.Connection, email: str) -> bool:
    """Used up or revoked, its Activation link goes with it. False if there is no such
    Invitation."""
    conn.execute("DELETE FROM activation_links WHERE email = ?", (email,))
    return conn.execute("DELETE FROM invitations WHERE email = ?", (email,)).rowcount > 0


def _exists(conn: sqlite3.Connection, email: str) -> bool:
    return conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone() is not None


def _user(row: sqlite3.Row) -> User:
    return User(
        email=row["email"],
        role=Role(row["role"]),
        runtime_id=row["runtime_id"],
        privileged_projects=bool(row["privileged_projects"]),
    )
