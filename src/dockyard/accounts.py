"""Accounts: Users and their role. An email that is not a User gets nothing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .database import Database


class Role(StrEnum):
    ADMINISTRATOR = "administrator"
    USER = "user"


@dataclass(frozen=True)
class User:
    email: str
    role: Role
    runtime_id: str  # names the User's resources in the Runtime; never reused


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

    def find_user(self, email: str) -> User | None:
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT email, role, runtime_id FROM users WHERE email = ?",
                (normalise_email(email),),
            ).fetchone()
        if row is None:
            return None
        return User(email=row["email"], role=Role(row["role"]), runtime_id=row["runtime_id"])
