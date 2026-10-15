"""Git Credentials: the GitHub and GitLab tokens a User keeps in their profile.

A token is encrypted with the Instance secret key as it is saved, and nothing here ever hands it
back: a Git Credential is known by its label and provider only.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from cryptography.fernet import Fernet

from .accounts import User
from .database import Database
from .providers import Provider

LABEL_MAX_LENGTH = 60
TOKEN_MAX_LENGTH = 1024
# What GitHub and GitLab tokens are made of. Nothing else gets through: the token ends up written
# into the gh and glab configuration files of the Projects that use it.
TOKEN = re.compile(r"^[A-Za-z0-9_.-]+$")


class Refusal(StrEnum):
    LABEL_MISSING = "label_missing"
    LABEL_INVALID = "label_invalid"
    TOKEN_MISSING = "token_missing"
    TOKEN_INVALID = "token_invalid"


class GitCredentialRefused(Exception):
    def __init__(self, reason: Refusal) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class GitCredential:
    id: str
    label: str
    provider: Provider
    created_at: str
    updated_at: str


class GitCredentials:
    def __init__(self, database: Database, secret_key: str) -> None:
        self._db = database
        self._fernet = Fernet(secret_key)

    def create(self, owner: User, *, label: str, provider: Provider, token: str) -> GitCredential:
        """Raises GitCredentialRefused."""
        label = valid_label(label)
        token = valid_token(token)
        with self._db.transaction() as conn:
            row = conn.execute(
                "INSERT INTO git_credentials (owner, label, provider, token) VALUES (?, ?, ?, ?)"
                f" RETURNING {_COLUMNS}",
                (owner.runtime_id, label, provider, self._encrypt(token)),
            ).fetchone()
        return _credential(row)

    def list(self, owner: User) -> list[GitCredential]:
        with self._db.transaction() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM git_credentials WHERE owner = ?"
                " ORDER BY created_at, rowid",
                (owner.runtime_id,),
            ).fetchall()
        return [_credential(row) for row in rows]

    def get(self, owner: User, credential: str) -> GitCredential | None:
        with self._db.transaction() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM git_credentials WHERE owner = ? AND id = ?",
                (owner.runtime_id, credential),
            ).fetchone()
        return _credential(row) if row else None

    def update(
        self, owner: User, credential: str, *, label: str | None = None, token: str | None = None
    ) -> GitCredential | None:
        """Rename it, rotate its token, or both. None if not found.

        Raises GitCredentialRefused.
        """
        changes: dict[str, str | bytes] = {}
        if label is not None:
            changes["label"] = valid_label(label)
        if token is not None:
            changes["token"] = self._encrypt(valid_token(token))
        assignments = "".join(f"{column} = ?, " for column in changes)
        with self._db.transaction() as conn:
            row = conn.execute(
                f"UPDATE git_credentials SET {assignments}"
                "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
                f" WHERE owner = ? AND id = ? RETURNING {_COLUMNS}",
                (*changes.values(), owner.runtime_id, credential),
            ).fetchone()
        return _credential(row) if row else None

    def delete(self, owner: User, credential: str) -> bool:
        """False if not found."""
        with self._db.transaction() as conn:
            deleted = conn.execute(
                "DELETE FROM git_credentials WHERE owner = ? AND id = ?",
                (owner.runtime_id, credential),
            )
        return deleted.rowcount > 0

    def _encrypt(self, token: str) -> bytes:
        return self._fernet.encrypt(token.encode())


def valid_label(label: str) -> str:
    label = label.strip()
    if not label:
        raise GitCredentialRefused(Refusal.LABEL_MISSING)
    if len(label) > LABEL_MAX_LENGTH or not label.isprintable():
        raise GitCredentialRefused(Refusal.LABEL_INVALID)
    return label


def valid_token(token: str) -> str:
    token = token.strip()  # as pasted, often with a line break
    if not token:
        raise GitCredentialRefused(Refusal.TOKEN_MISSING)
    if len(token) > TOKEN_MAX_LENGTH or not TOKEN.match(token):
        raise GitCredentialRefused(Refusal.TOKEN_INVALID)
    return token


_COLUMNS = "id, label, provider, created_at, updated_at"  # never the token


def _credential(row: sqlite3.Row) -> GitCredential:
    return GitCredential(
        id=row["id"],
        label=row["label"],
        provider=Provider(row["provider"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
