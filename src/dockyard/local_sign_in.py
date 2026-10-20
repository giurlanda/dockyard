"""Local sign-in: Users' passwords, signing in with them, and the sessions that follow; the
Activation links with which invited people choose their password, and Users who forgot theirs
a new one; the Second factor, which asks for a code from an authenticator app after the password.

Only the local listener uses it (ADR 0004), but for a User who came in through the
authentication proxy setting their first password from their own page. Neither passwords, nor
session tokens, nor recovery codes are stored: only what proves them. The authenticator app's
secret has to be read back, so it is stored encrypted with the Instance secret key.
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

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.hashes import SHA1
from cryptography.hazmat.primitives.twofactor.totp import TOTP

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
# The Second factor: authenticator codes as RFC 6238 has them, accepted one step early or late,
# for a clock that is a little off. The code step lets this long between password and code.
CODE_DIGITS = 6
CODE_STEP_SECONDS = 30
CODE_STEP_LIFETIME = timedelta(minutes=5)
RECOVERY_CODES = 10
# Twelve characters of these, about 60 bits: never told apart from a 6-digit authenticator code,
# none of 0 and o, 1 and l to mistake for each other when typed from paper.
_RECOVERY_CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
_RECOVERY_CODE_GROUPS = 3
_RECOVERY_CODE_GROUP = 4
T = TypeVar("T")
# The Activation link, or code step, with this token hash that has not expired at this time.
_LIVE_TOKEN = "token_hash = ? AND julianday(expires_at) > julianday(?)"

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


class ActivationLinkKind(StrEnum):
    INVITATION = "invitation"  # the invited person chooses their password and becomes a User
    RESET = "reset"  # the User, who forgot their password, chooses a new one


@dataclass(frozen=True)
class ActivationLink:
    email: str
    kind: ActivationLinkKind


# Whom each kind of Activation link is made for: the table they are found in.
_LINKED_TO = {ActivationLinkKind.INVITATION: "invitations", ActivationLinkKind.RESET: "users"}


class ActivationLinkInvalid(Exception):
    """The Activation link expired, was used, or was voided: by a new one, by revoking its
    Invitation, by the person arriving through the authentication proxy first, or by removing
    its User."""


class SignInRefused(Exception):
    """The email or the password is wrong, told apart by nothing. retry_in: how long until the
    next attempt is checked, once failures slow them down."""

    def __init__(self, retry_in: timedelta | None) -> None:
        self.retry_in = retry_in
        super().__init__(retry_in)


class SlowedDown(SignInRefused):
    """Too many failures: refused without checking the password."""


class CodeStepOver(Exception):
    """The code step expired or was used: the person signs in again, from the password."""


class HasSecondFactor(Exception):
    """The User has a Second factor already: they turn it off before turning on a new one."""


class NoSecondFactor(Exception):
    """The User has no Second factor (any more)."""


class NothingToConfirm(Exception):
    """No new Second factor waits for a code: confirmed or replaced meanwhile."""


class CodeWrong(Exception):
    """The code is not the authenticator app's code for the new Second factor, right now."""


@dataclass(frozen=True)
class SignedIn:
    session: str  # the new session's token


@dataclass(frozen=True)
class CodeAsked:
    """The password was right, and the User's Second factor asks for a code next."""

    step: str  # the code step's token


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
    def __init__(self, database: Database, secret_key: str, clock: Clock) -> None:
        self._db = database
        self._fernet = Fernet(secret_key)  # for the authenticator apps' secrets
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
            _sign_out(conn, email)

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
            _sign_out(conn, email, keep=keep)

    def _stored_password(self, email: str) -> str | None:
        with self._db.transaction() as conn:
            row = conn.execute("SELECT password FROM users WHERE email = ?", (email,)).fetchone()
        return row["password"] if row else None

    def new_activation_link(self, email: str) -> str | None:
        """The token of a new Activation link for the person's pending Invitation, replacing the
        link they had. None when there is no such Invitation."""
        return self._new_activation_link(email, ActivationLinkKind.INVITATION)

    def new_activation_link_to_reset(self, email: str) -> str | None:
        """The token of a new Activation link with which the User chooses a new password,
        replacing the link they had. None when there is no such User."""
        return self._new_activation_link(email, ActivationLinkKind.RESET)

    def _new_activation_link(self, email: str, kind: ActivationLinkKind) -> str | None:
        token = secrets.token_urlsafe(32)
        expires_at = self._clock.now() + ACTIVATION_LINK_LIFETIME
        with self._db.transaction() as conn:
            made = conn.execute(
                "INSERT INTO activation_links (token_hash, email, kind, expires_at)"
                f" SELECT ?, email, ?, ? FROM {_LINKED_TO[kind]} WHERE email = ?"
                " ON CONFLICT (email) DO UPDATE SET token_hash = excluded.token_hash,"
                " kind = excluded.kind, expires_at = excluded.expires_at",
                (_token_hash(token), kind, expires_at.isoformat(), normalise_email(email)),
            ).rowcount
        return token if made else None

    def activation_link(self, token: str) -> ActivationLink | None:
        """Who the Activation link is for, and why; None when it no longer works."""
        with self._db.transaction() as conn:
            row = conn.execute(
                f"SELECT email, kind FROM activation_links WHERE {_LIVE_TOKEN}",
                (_token_hash(token), self._clock.now().isoformat()),
            ).fetchone()
        return ActivationLink(row["email"], ActivationLinkKind(row["kind"])) if row else None

    def activate(self, token: str, password: str) -> SignedIn | CodeAsked:
        """Use up the Activation link. One for an Invitation uses it up too: the person becomes a
        User with this password, signed in. One for a reset gives the User this password and
        signs them out everywhere; a User with a Second factor then gives a code to sign in.

        Raises ActivationLinkInvalid, PasswordRefused.
        """
        link = self.activation_link(token)
        if link is None:
            raise ActivationLinkInvalid
        email = link.email
        hashed = _hash_password(_allowed(password, email))
        now = self._clock.now()
        with self._db.transaction() as conn:
            # A link opened twice at once works once.
            used = conn.execute(
                f"DELETE FROM activation_links WHERE {_LIVE_TOKEN} RETURNING kind",
                (_token_hash(token), now.isoformat()),
            ).fetchone()
            if used is None:
                raise ActivationLinkInvalid
            if used["kind"] == ActivationLinkKind.INVITATION:
                # Whichever comes first, this or the person arriving through the authentication
                # proxy, uses up the Invitation.
                if not conn.execute("DELETE FROM invitations WHERE email = ?", (email,)).rowcount:
                    raise ActivationLinkInvalid
                conn.execute(
                    "INSERT INTO users (email, role, password) VALUES (?, ?, ?)",
                    (email, Role.USER, hashed),
                )
            else:
                # Whoever knew the old password is signed out. A Second factor stays: the link
                # alone does not hand over an account it protects.
                changed = conn.execute(
                    "UPDATE users SET password = ? WHERE email = ?", (hashed, email)
                ).rowcount
                if not changed:
                    raise ActivationLinkInvalid
                _sign_out(conn, email)
            return _signed_in_or_asked(conn, email, now)

    def sign_in(self, email: str, password: str, client: str) -> SignedIn | CodeAsked:
        """A new Sign-in session, or the code step when the User has a Second factor. Whatever
        is wrong (the email, the password, or the person is not a User) is told apart neither by
        the refusal nor by the time it takes.

        Raises SignInRefused, SlowedDown.
        """
        email = normalise_email(email)

        def checked(now: datetime) -> SignedIn | CodeAsked | None:
            return self._signed_in_if_right(email, password, now)

        outcome = self._attempt(email, client, "Local sign-in", checked)
        assert isinstance(outcome, SignedIn | CodeAsked)  # mypy infers no union through T | None
        return outcome

    def code_step(self, token: str) -> str | None:
        """The email the code step signs in; None when it is over."""
        with self._db.transaction() as conn:
            row = conn.execute(
                f"SELECT email FROM code_steps WHERE {_LIVE_TOKEN}",
                (_token_hash(token), self._clock.now().isoformat()),
            ).fetchone()
        return row["email"] if row else None

    def complete_sign_in(self, step: str, code: str, client: str) -> str:
        """The code step ends with a code from the authenticator app, or an unused recovery
        code: a new session's token. A wrong code is a failed sign-in, and the step goes on.

        Raises CodeStepOver, SignInRefused, SlowedDown.
        """
        email = self.code_step(step)
        if email is None:
            raise CodeStepOver

        def checked(now: datetime) -> str | None:
            with self._db.transaction() as conn:
                if not self._code_accepted(conn, email, code, now):
                    return None
                if not conn.execute(
                    f"DELETE FROM code_steps WHERE {_LIVE_TOKEN}",
                    (_token_hash(step), now.isoformat()),
                ).rowcount:
                    raise CodeStepOver  # used meanwhile: rolled back, the code stays unused
                return _new_session(conn, email, now)

        return self._attempt(email, client, "Second factor", checked)

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
            # Halfway, at the code step, the email's failures still count: else the password
            # would forget the wrong codes guessed with it.
            if not isinstance(result, CodeAsked):
                with self._db.transaction() as conn:
                    conn.execute("DELETE FROM sign_in_throttling WHERE key = ?", (email_key,))
            return result

    def _signed_in_if_right(
        self, email: str, password: str, now: datetime
    ) -> SignedIn | CodeAsked | None:
        """When the password is the User's, a new session or the code step; None otherwise."""
        stored = self._stored_password(email)
        if not _password_matches(password, stored or self._nobodys_password):
            return None
        if stored is None:
            return None
        with self._db.transaction() as conn:
            return _signed_in_or_asked(conn, email, now)

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

    def has_second_factor(self, email: str) -> bool:
        return self.recovery_codes_left(email) is not None

    def recovery_codes_left(self, email: str) -> int | None:
        """How many recovery codes the User has not used; None when they have no Second factor."""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT second_factor IS NOT NULL AS on_, (SELECT count(*) FROM recovery_codes"
                " WHERE recovery_codes.email = users.email AND NOT used) AS left_"
                " FROM users WHERE email = ?",
                (normalise_email(email),),
            ).fetchone()
        return row["left_"] if row and row["on_"] else None

    def start_second_factor(self, email: str, password: str, *, client: str) -> str:
        """The User, by giving their password, gets a new secret for their authenticator app, as
        the app takes it typed (base32). It waits for a code that confirms it, replacing any
        that waited. A wrong password is a failed sign-in: someone at the User's unlocked device
        cannot lock them out with an app of their own.

        Raises SignInRefused, SlowedDown, HasSecondFactor.
        """
        email = normalise_email(email)
        if self.has_second_factor(email):
            raise HasSecondFactor
        secret = secrets.token_bytes(20)  # 160 bits, as RFC 4226 recommends for SHA-1

        def checked(now: datetime) -> bool | None:
            stored = self._stored_password(email)
            if stored is None or not _password_matches(password, stored):
                return None
            with self._db.transaction() as conn:
                if not conn.execute(
                    "UPDATE users SET pending_second_factor = ?"
                    " WHERE email = ? AND second_factor IS NULL",
                    (self._fernet.encrypt(secret), email),
                ).rowcount:
                    raise HasSecondFactor  # turned on meanwhile
            return True

        self._attempt(email, client, "Turning on the Second factor", checked)
        return _base32(secret)

    def pending_second_factor(self, email: str) -> str | None:
        """The new secret waiting for a code that confirms it, if one does."""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT pending_second_factor FROM users WHERE email = ?",
                (normalise_email(email),),
            ).fetchone()
        if row is None or row["pending_second_factor"] is None:
            return None
        return _base32(self._fernet.decrypt(row["pending_second_factor"]))

    def confirm_second_factor(self, email: str, code: str) -> list[str]:
        """The code from the authenticator app proves it holds the new secret: from now on, Local
        sign-in asks for a code. The new recovery codes, to show once.

        Raises CodeWrong, NothingToConfirm.
        """
        email = normalise_email(email)
        now = self._clock.now()
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT pending_second_factor FROM users WHERE email = ?", (email,)
            ).fetchone()
            if row is None or row["pending_second_factor"] is None:
                raise NothingToConfirm
            pending = row["pending_second_factor"]
            step = _accepted_step(self._fernet.decrypt(pending), code, now, after=None)
            if step is None:
                raise CodeWrong
            confirmed = conn.execute(
                "UPDATE users SET second_factor = pending_second_factor,"
                " pending_second_factor = NULL, second_factor_step = ?"
                " WHERE email = ? AND pending_second_factor = ?",
                (step, email, pending),
            ).rowcount
            if not confirmed:
                raise NothingToConfirm
            return _new_recovery_codes(conn, email)

    def new_recovery_codes(self, email: str, code_or_password: str, *, client: str) -> list[str]:
        """The User, by giving a code from the authenticator app or their password, gets new
        recovery codes, to show once; the old ones no longer work. A wrong one is a failed
        sign-in.

        Raises SignInRefused, SlowedDown, NoSecondFactor.
        """
        return self._with_code_or_password(
            email, code_or_password, client, "New recovery codes", _new_recovery_codes
        )

    def turn_off_second_factor(self, email: str, code_or_password: str, *, client: str) -> None:
        """The User turns their Second factor off by giving a code from the authenticator app or
        their password. A wrong one is a failed sign-in.

        Raises SignInRefused, SlowedDown, NoSecondFactor.
        """
        self._with_code_or_password(
            email, code_or_password, client, "Turning off the Second factor", _remove_second_factor
        )

    def _with_code_or_password(
        self,
        email: str,
        code_or_password: str,
        client: str,
        what: str,
        change: Callable[[sqlite3.Connection, str], T],
    ) -> T:
        """What `change` makes of the User's Second factor, once they gave a code from the
        authenticator app or their password: what someone at their unlocked device has not.

        Raises SignInRefused, SlowedDown, NoSecondFactor.
        """
        email = normalise_email(email)
        if not self.has_second_factor(email):
            raise NoSecondFactor
        given = "".join(code_or_password.split())

        # In a tuple: what `change` makes may be None, which _attempt takes for a failure.
        def checked(now: datetime) -> tuple[T] | None:
            if _is_authenticator_code(given):
                with self._db.transaction() as conn:
                    right = self._authenticator_code_accepted(conn, email, given, now)
            else:  # never a password: passwords are longer
                stored = self._stored_password(email)
                right = stored is not None and _password_matches(code_or_password, stored)
            if not right:
                return None
            with self._db.transaction() as conn:
                if not conn.execute(
                    "SELECT 1 FROM users WHERE email = ? AND second_factor IS NOT NULL", (email,)
                ).fetchone():
                    raise NoSecondFactor  # turned off meanwhile
                return (change(conn, email),)

        (changed,) = self._attempt(email, client, what, checked)
        return changed

    def _code_accepted(
        self, conn: sqlite3.Connection, email: str, code: str, now: datetime
    ) -> bool:
        """Uses up the code when it is the User's: a code from the authenticator app (and with
        it every earlier one) or an unused recovery code."""
        code = "".join(code.split())
        if _is_authenticator_code(code):
            return self._authenticator_code_accepted(conn, email, code, now)
        return bool(
            conn.execute(
                "UPDATE recovery_codes SET used = 1 WHERE email = ? AND code_hash = ? AND NOT used",
                (email, _recovery_code_hash(code)),
            ).rowcount
        )

    def _authenticator_code_accepted(
        self, conn: sqlite3.Connection, email: str, code: str, now: datetime
    ) -> bool:
        row = conn.execute(
            "SELECT second_factor, second_factor_step FROM users WHERE email = ?", (email,)
        ).fetchone()
        if row is None or row["second_factor"] is None:
            return False
        secret = self._fernet.decrypt(row["second_factor"])
        step = _accepted_step(secret, code, now, after=row["second_factor_step"])
        if step is None:
            return False
        # Two at once with the same code: only one moves the step on.
        return bool(
            conn.execute(
                "UPDATE users SET second_factor_step = ? WHERE email = ?"
                " AND (second_factor_step IS NULL OR second_factor_step < ?)",
                (step, email, step),
            ).rowcount
        )


def _allowed(password: str, email: str) -> str:
    """Raises PasswordRefused."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordRefused(PasswordRule.TOO_SHORT)
    if password.lower() == email:
        raise PasswordRefused(PasswordRule.EQUALS_EMAIL)
    return password


def _sign_out(conn: sqlite3.Connection, email: str, *, keep: str | None = None) -> None:
    """Every Sign-in session of the User ends, but the one `keep` is the token of, and so does
    every code step halfway to one."""
    conn.execute(
        "DELETE FROM sessions WHERE email = ? AND token_hash IS NOT ?",
        (email, keep and _token_hash(keep)),
    )
    conn.execute("DELETE FROM code_steps WHERE email = ?", (email,))


def _signed_in_or_asked(
    conn: sqlite3.Connection, email: str, now: datetime
) -> SignedIn | CodeAsked:
    """The User, who gave the right password, is signed in; or asked for a code next, when they
    have a Second factor."""
    if conn.execute(
        "SELECT 1 FROM users WHERE email = ? AND second_factor IS NOT NULL", (email,)
    ).fetchone():
        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO code_steps (token_hash, email, expires_at) VALUES (?, ?, ?)",
            (_token_hash(token), email, (now + CODE_STEP_LIFETIME).isoformat()),
        )
        return CodeAsked(token)
    return SignedIn(_new_session(conn, email, now))


def _new_session(conn: sqlite3.Connection, email: str, now: datetime) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (token_hash, email, created_at, last_used_at) VALUES (?, ?, ?, ?)",
        (_token_hash(token), email, now.isoformat(), now.isoformat()),
    )
    return token


def _remove_second_factor(conn: sqlite3.Connection, email: str) -> None:
    conn.execute(
        "UPDATE users SET second_factor = NULL, pending_second_factor = NULL,"
        " second_factor_step = NULL WHERE email = ?",
        (email,),
    )
    conn.execute("DELETE FROM recovery_codes WHERE email = ?", (email,))
    conn.execute("DELETE FROM code_steps WHERE email = ?", (email,))


def _is_authenticator_code(code: str) -> bool:
    return len(code) == CODE_DIGITS and code.isascii() and code.isdigit()


def _accepted_step(secret: bytes, code: str, now: datetime, *, after: int | None) -> int | None:
    """The time step, one either side of now's, the code is the authenticator app's for; None
    when it is none of them, or only one no later than `after`, whose code was used already."""
    code = "".join(code.split())
    if not _is_authenticator_code(code):
        return None
    totp = TOTP(secret, CODE_DIGITS, SHA1(), CODE_STEP_SECONDS)
    current = int(now.timestamp()) // CODE_STEP_SECONDS
    for step in (current - 1, current, current + 1):
        expected = totp.generate(step * CODE_STEP_SECONDS)
        if (after is None or step > after) and hmac.compare_digest(expected, code.encode()):
            return step
    return None


def _new_recovery_codes(conn: sqlite3.Connection, email: str) -> list[str]:
    """Replaces the User's recovery codes: the new ones, as they are shown."""
    codes = [
        "-".join(
            "".join(secrets.choice(_RECOVERY_CODE_ALPHABET) for _ in range(_RECOVERY_CODE_GROUP))
            for _ in range(_RECOVERY_CODE_GROUPS)
        )
        for _ in range(RECOVERY_CODES)
    ]
    conn.execute("DELETE FROM recovery_codes WHERE email = ?", (email,))
    conn.executemany(
        "INSERT INTO recovery_codes (email, code_hash) VALUES (?, ?)",
        [(email, _recovery_code_hash(code)) for code in codes],
    )
    return codes


def _recovery_code_hash(code: str) -> str:
    """Whatever the case, the spaces and the dashes the person typed it with."""
    return _token_hash("".join(c for c in code.lower() if c.isalnum()))


def _base32(secret: bytes) -> str:
    return base64.b32encode(secret).decode().rstrip("=")


def _longest(waits: list[timedelta]) -> timedelta | None:
    return max(waits, default=None)


def _delay_after(failures: int) -> timedelta | None:
    if failures < FAILURES_BEFORE_DELAY:
        return None
    doublings = min(failures - FAILURES_BEFORE_DELAY, 10)  # 2**10 is past the longest already
    return min(FIRST_DELAY * (1 << doublings), LONGEST_DELAY)
