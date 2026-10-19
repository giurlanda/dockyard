"""Local sign-in: Users' passwords, signing in with them, and the sessions that follow.

Only the local listener uses it (ADR 0004). Neither passwords nor session tokens are stored: only
what proves them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import threading
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
# Failed sign-ins, per email and per client address: after this many in a row, each further
# attempt waits a delay that doubles from the first up to the longest. Nothing locks for good.
FAILURES_BEFORE_DELAY = 5
FIRST_DELAY = timedelta(seconds=30)
LONGEST_DELAY = timedelta(minutes=15)
FAILURES_FORGOTTEN_AFTER = timedelta(days=1)  # a day without one: counting starts again

log = logging.getLogger(__name__)


class PasswordRule(StrEnum):
    TOO_SHORT = "too_short"
    EQUALS_EMAIL = "equals_email"


class PasswordRefused(Exception):
    def __init__(self, rule: PasswordRule) -> None:
        self.rule = rule
        super().__init__(rule)


class NotAUser(Exception):
    """Only a User has a password."""


class SignInRefused(Exception):
    """The email or the password is wrong, told apart by nothing. retry_in: how long until the
    next attempt is checked, once failures slow them down."""

    def __init__(self, retry_in: timedelta | None) -> None:
        self.retry_in = retry_in
        super().__init__(retry_in)


class SlowedDown(SignInRefused):
    """Too many failures: refused without checking the password."""


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
        # One attempt at a time: otherwise attempts sent together would all be checked before
        # any of their failures counted. Each slow check is one a failure is counted for.
        self._attempts = threading.Lock()

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

    def sign_in(self, email: str, password: str, client: str) -> str:
        """A new session's token. Whatever is wrong (the email, the password, or the person is
        not a User) is told apart neither by the refusal nor by the time it takes.

        Raises SignInRefused, SlowedDown.
        """
        email = normalise_email(email)
        email_key, client_key = f"email:{email}", f"client:{client}"
        with self._attempts:
            now = self._clock.now()
            if retry_in := self._retry_in((email_key, client_key), now):
                log.warning("Local sign-in slowed down for %r from %s", email, client)
                raise SlowedDown(retry_in)
            token = self._session_if_right(email, password, now)
            if token is None:
                log.warning("Local sign-in failed for %r from %s", email, client)
                raise SignInRefused(self._count_failure((email_key, client_key), now))
            with self._db.transaction() as conn:
                conn.execute("DELETE FROM sign_in_throttling WHERE key = ?", (email_key,))
            return token

    def _session_if_right(self, email: str, password: str, now: datetime) -> str | None:
        """A new session's token when the password is the User's; None otherwise."""
        with self._db.transaction() as conn:
            row = conn.execute("SELECT password FROM users WHERE email = ?", (email,)).fetchone()
        stored = row["password"] if row else None
        if not _password_matches(password, stored or self._nobodys_password):
            return None
        if stored is None:
            return None
        token = secrets.token_urlsafe(32)
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO sessions (token_hash, email, created_at, last_used_at)"
                " VALUES (?, ?, ?, ?)",
                (_token_hash(token), email, now.isoformat(), now.isoformat()),
            )
        return token

    def _retry_in(self, keys: tuple[str, str], now: datetime) -> timedelta | None:
        with self._db.transaction() as conn:
            rows = conn.execute(
                "SELECT retry_after FROM sign_in_throttling"
                " WHERE key IN (?, ?) AND retry_after IS NOT NULL",
                keys,
            ).fetchall()
        waits = [datetime.fromisoformat(row["retry_after"]) - now for row in rows]
        return _longest([wait for wait in waits if wait > timedelta(0)])

    def _count_failure(self, keys: tuple[str, str], now: datetime) -> timedelta | None:
        """One more failure for each key; how long the next attempt waits, if it does."""
        delays = []
        with self._db.transaction() as conn:
            conn.execute(
                "DELETE FROM sign_in_throttling WHERE julianday(last_failed_at) <= julianday(?)",
                ((now - FAILURES_FORGOTTEN_AFTER).isoformat(),),
            )
            for key in keys:
                (failures,) = conn.execute(
                    "INSERT INTO sign_in_throttling (key, failures, last_failed_at)"
                    " VALUES (?, 1, ?) ON CONFLICT (key) DO UPDATE"
                    " SET failures = failures + 1, last_failed_at = excluded.last_failed_at"
                    " RETURNING failures",
                    (key, now.isoformat()),
                ).fetchone()
                delay = _delay_after(failures)
                conn.execute(
                    "UPDATE sign_in_throttling SET retry_after = ? WHERE key = ?",
                    (delay and (now + delay).isoformat(), key),
                )
                if delay:
                    delays.append(delay)
        return _longest(delays)

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


def _longest(waits: list[timedelta]) -> timedelta | None:
    return max(waits, default=None)


def _delay_after(failures: int) -> timedelta | None:
    if failures < FAILURES_BEFORE_DELAY:
        return None
    doublings = min(failures - FAILURES_BEFORE_DELAY, 10)  # 2**10 is past the longest already
    return min(FIRST_DELAY * (1 << doublings), LONGEST_DELAY)
