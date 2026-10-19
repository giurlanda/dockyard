"""Local sign-in: Users' passwords, signing in with them, and the sessions that follow; the
Activation links with which invited people choose their password.

Only the local listener uses it (ADR 0004), but for a User who came in through the
authentication proxy setting their first password from their own page. Neither passwords nor
session tokens are stored: only what proves them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TypeVar

from .accounts import Role, normalise_email
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
ACTIVATION_LINK_LIFETIME = timedelta(days=7)
T = TypeVar("T")
# The Activation link with this token hash that has not expired at this time.
_LIVE_LINK = "token_hash = ? AND julianday(expires_at) > julianday(?)"

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


class HasPassword(Exception):
    """The User already has a password: only giving it changes it."""


class ActivationLinkInvalid(Exception):
    """The Activation link expired, was used, or was voided: by a new one, by revoking its
    Invitation, or by the person arriving through the authentication proxy first."""


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
        hashed = _hash_password(_allowed(password, email))
        with self._db.transaction() as conn:
            changed = conn.execute(
                "UPDATE users SET password = ? WHERE email = ?", (hashed, email)
            ).rowcount
            if not changed:
                raise NotAUser
            conn.execute("DELETE FROM sessions WHERE email = ?", (email,))

    def has_password(self, email: str) -> bool:
        return self._stored_password(normalise_email(email)) is not None

    def set_first_password(self, email: str, password: str) -> None:
        """The User, who has no password yet, sets one from their own page.

        Raises PasswordRefused, HasPassword, NotAUser.
        """
        email = normalise_email(email)
        self._replace_password(email, None, _hash_password(_allowed(password, email)), keep=None)

    def change_password(
        self, email: str, current: str, new: str, *, client: str, keep: str | None
    ) -> None:
        """The User, from their own page, replaces their password by giving it. A wrong one is a
        failed sign-in. Every other Sign-in session of theirs ends: all but the one `keep` is
        the token of.

        Raises SignInRefused, SlowedDown, PasswordRefused, HasPassword (changed meanwhile),
        NotAUser.
        """
        email = normalise_email(email)

        def checked(_: datetime) -> str | None:
            stored = self._stored_password(email)
            return stored if stored and _password_matches(current, stored) else None

        stored = self._attempt(email, client, "Password change", checked)
        self._replace_password(email, stored, _hash_password(_allowed(new, email)), keep=keep)

    def _replace_password(
        self, email: str, stored: str | None, hashed: str, *, keep: str | None
    ) -> None:
        """Only the password that was checked is replaced, not one set meanwhile.

        Raises HasPassword, NotAUser.
        """
        with self._db.transaction() as conn:
            changed = conn.execute(
                "UPDATE users SET password = ? WHERE email = ? AND password IS ?",
                (hashed, email, stored),
            ).rowcount
            if not changed:
                if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                    raise HasPassword
                raise NotAUser
            conn.execute(
                "DELETE FROM sessions WHERE email = ? AND token_hash IS NOT ?",
                (email, keep and _token_hash(keep)),
            )

    def _stored_password(self, email: str) -> str | None:
        with self._db.transaction() as conn:
            row = conn.execute("SELECT password FROM users WHERE email = ?", (email,)).fetchone()
        return row["password"] if row else None

    def new_activation_link(self, email: str) -> str | None:
        """The token of a new Activation link for the person's pending Invitation, replacing the
        link they had. None when there is no such Invitation."""
        token = secrets.token_urlsafe(32)
        expires_at = self._clock.now() + ACTIVATION_LINK_LIFETIME
        with self._db.transaction() as conn:
            made = conn.execute(
                "INSERT INTO activation_links (token_hash, email, kind, expires_at)"
                " SELECT ?, email, 'invitation', ? FROM invitations WHERE email = ?"
                " ON CONFLICT (email) DO UPDATE SET token_hash = excluded.token_hash,"
                " kind = excluded.kind, expires_at = excluded.expires_at",
                (_token_hash(token), expires_at.isoformat(), normalise_email(email)),
            ).rowcount
        return token if made else None

    def activation_link_email(self, token: str) -> str | None:
        """The email the Activation link is for; None when it no longer works."""
        with self._db.transaction() as conn:
            row = conn.execute(
                f"SELECT email FROM activation_links WHERE {_LIVE_LINK}",
                (_token_hash(token), self._clock.now().isoformat()),
            ).fetchone()
        return row["email"] if row else None

    def activate(self, token: str, password: str) -> str:
        """Use up the Activation link and its Invitation: the person becomes a User with this
        password. A new session's token.

        Raises ActivationLinkInvalid, PasswordRefused.
        """
        email = self.activation_link_email(token)
        if email is None:
            raise ActivationLinkInvalid
        hashed = _hash_password(_allowed(password, email))
        now = self._clock.now()
        with self._db.transaction() as conn:
            # Whichever comes first, this or the person arriving through the authentication proxy,
            # uses up the Invitation; a link opened twice at once works once.
            used = conn.execute(
                f"DELETE FROM activation_links WHERE {_LIVE_LINK}",
                (_token_hash(token), now.isoformat()),
            ).rowcount
            invited = conn.execute("DELETE FROM invitations WHERE email = ?", (email,)).rowcount
            if not (used and invited):
                raise ActivationLinkInvalid
            conn.execute(
                "INSERT INTO users (email, role, password) VALUES (?, ?, ?)",
                (email, Role.USER, hashed),
            )
            return _new_session(conn, email, now)

    def sign_in(self, email: str, password: str, client: str) -> str:
        """A new session's token. Whatever is wrong (the email, the password, or the person is
        not a User) is told apart neither by the refusal nor by the time it takes.

        Raises SignInRefused, SlowedDown.
        """
        email = normalise_email(email)
        return self._attempt(
            email, client, "Local sign-in", lambda now: self._session_if_right(email, password, now)
        )

    def _attempt(
        self, email: str, client: str, what: str, check: Callable[[datetime], T | None]
    ) -> T:
        """What `check` gives when the password is right; None is a failure, counted for the
        email and for the client address. Slowed down once failures add up.

        Raises SignInRefused, SlowedDown.
        """
        email_key, client_key = f"email:{email}", f"client:{client}"
        with self._attempts:
            now = self._clock.now()
            if retry_in := self._retry_in((email_key, client_key), now):
                log.warning("%s slowed down for %r from %s", what, email, client)
                raise SlowedDown(retry_in)
            result = check(now)
            if result is None:
                log.warning("%s failed for %r from %s", what, email, client)
                raise SignInRefused(self._count_failure((email_key, client_key), now))
            with self._db.transaction() as conn:
                conn.execute("DELETE FROM sign_in_throttling WHERE key = ?", (email_key,))
            return result

    def _session_if_right(self, email: str, password: str, now: datetime) -> str | None:
        """A new session's token when the password is the User's; None otherwise."""
        stored = self._stored_password(email)
        if not _password_matches(password, stored or self._nobodys_password):
            return None
        if stored is None:
            return None
        with self._db.transaction() as conn:
            return _new_session(conn, email, now)

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


def _allowed(password: str, email: str) -> str:
    """Raises PasswordRefused."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordRefused(PasswordRule.TOO_SHORT)
    if password.lower() == email:
        raise PasswordRefused(PasswordRule.EQUALS_EMAIL)
    return password


def _new_session(conn: sqlite3.Connection, email: str, now: datetime) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (token_hash, email, created_at, last_used_at) VALUES (?, ?, ?, ?)",
        (_token_hash(token), email, now.isoformat(), now.isoformat()),
    )
    return token


def _longest(waits: list[timedelta]) -> timedelta | None:
    return max(waits, default=None)


def _delay_after(failures: int) -> timedelta | None:
    if failures < FAILURES_BEFORE_DELAY:
        return None
    doublings = min(failures - FAILURES_BEFORE_DELAY, 10)  # 2**10 is past the longest already
    return min(FIRST_DELAY * (1 << doublings), LONGEST_DELAY)
