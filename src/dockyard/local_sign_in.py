"""Local sign-in: Users' passwords, signing in with them, and the sessions that follow.

Only the local listener uses it (ADR 0004). Neither passwords nor session tokens are stored: only
what proves them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from .accounts import normalise_email
from .clock import Clock
from .database import Database

MIN_PASSWORD_LENGTH = 12
SESSION_IDLE = timedelta(days=7)  # a session not used for this long is over
SESSION_LIFETIME = timedelta(days=30)  # and none lasts longer, however much it is used
SESSION_USE_RECORDED_EVERY = timedelta(minutes=1)  # not a write for every request


class PasswordRule(StrEnum):
    TOO_SHORT = "too_short"
    EQUALS_EMAIL = "equals_email"


class PasswordRefused(Exception):
    def __init__(self, rule: PasswordRule) -> None:
        self.rule = rule
        super().__init__(rule)


class NotAUser(Exception):
    """Only a User has a password."""


@dataclass(frozen=True)
class _Scrypt:
    """The standard library's scrypt; its parameters are stored with each hash, so new hashes can
    use stronger ones while the old ones still verify."""

    n: int = 2**15
    r: int = 8
    p: int = 1

    def hash(self, password: str, salt: bytes) -> bytes:
        return hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=self.n,
            r=self.r,
            p=self.p,
            maxmem=2 * 128 * self.r * self.n,
            dklen=32,
        )


def _hash_password(password: str) -> str:
    scrypt = _Scrypt()
    salt = secrets.token_bytes(16)
    return "$".join(
        (
            "scrypt",
            str(scrypt.n),
            str(scrypt.r),
            str(scrypt.p),
            _b64(salt),
            _b64(scrypt.hash(password, salt)),
        )
    )


def _password_matches(password: str, stored: str) -> bool:
    _, n, r, p, salt, expected = stored.split("$")
    computed = _Scrypt(int(n), int(r), int(p)).hash(password, base64.b64decode(salt))
    return hmac.compare_digest(computed, base64.b64decode(expected))


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class LocalSignIn:
    def __init__(self, database: Database, clock: Clock) -> None:
        self._db = database
        self._clock = clock
        # Checked when there is no password to check, so that takes as long as a wrong one. Made
        # now: made at the first unknown email, it would make that refusal slower.
        self._nobodys_password = _hash_password(secrets.token_urlsafe(16))

    def set_password(self, email: str, password: str) -> None:
        """Give the User a new password, signing them out everywhere.

        Raises PasswordRefused, NotAUser.
        """
        email = normalise_email(email)
        if len(password) < MIN_PASSWORD_LENGTH:
            raise PasswordRefused(PasswordRule.TOO_SHORT)
        if password.lower() == email:
            raise PasswordRefused(PasswordRule.EQUALS_EMAIL)
        hashed = _hash_password(password)
        with self._db.transaction() as conn:
            changed = conn.execute(
                "UPDATE users SET password = ? WHERE email = ?", (hashed, email)
            ).rowcount
            if not changed:
                raise NotAUser
            conn.execute("DELETE FROM sessions WHERE email = ?", (email,))

    def sign_in(self, email: str, password: str) -> str | None:
        """A new session's token, or None. Whatever is wrong (the email, the password, or the
        person is not a User) is told apart neither by the answer nor by the time it takes."""
        email = normalise_email(email)
        with self._db.transaction() as conn:
            row = conn.execute("SELECT password FROM users WHERE email = ?", (email,)).fetchone()
        stored = row["password"] if row else None
        if not _password_matches(password, stored or self._nobodys_password):
            return None
        if stored is None:
            return None
        token = secrets.token_urlsafe(32)
        now = self._clock.now().isoformat()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO sessions (token_hash, email, created_at, last_used_at)"
                " VALUES (?, ?, ?, ?)",
                (_token_hash(token), email, now, now),
            )
        return token

    def signed_in_email(self, token: str) -> str | None:
        """The email of the User the session belongs to; None when it is over. Using it keeps it
        going, up to its lifetime."""
        token_hash = _token_hash(token)
        now = self._clock.now()
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT email, created_at, last_used_at FROM sessions WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            created = datetime.fromisoformat(row["created_at"])
            last_used = datetime.fromisoformat(row["last_used_at"])
            if now - last_used >= SESSION_IDLE or now - created >= SESSION_LIFETIME:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
                return None
            if now - last_used >= SESSION_USE_RECORDED_EVERY:
                conn.execute(
                    "UPDATE sessions SET last_used_at = ? WHERE token_hash = ?",
                    (now.isoformat(), token_hash),
                )
        email: str = row["email"]
        return email

    def sign_out(self, token: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
