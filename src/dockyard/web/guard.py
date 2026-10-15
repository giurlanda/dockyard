"""Every HTTP request and WebSocket passes here.

Nothing gets through without a verified User, nor when another site acts on that User's behalf.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated
from urllib.parse import urlsplit

import anyio
from fastapi import Depends
from starlette.requests import HTTPConnection
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from ..accounts import Accounts, Role, User
from ..identity import Identity, IdentityUnavailable


class Refusal(StrEnum):
    UNAUTHENTICATED = "unauthenticated"
    NOT_INVITED = "not_invited"
    IDENTITY_UNAVAILABLE = "identity_unavailable"
    CROSS_SITE = "cross_site"

    @property
    def status_code(self) -> int:
        return _STATUS_CODES[self]

    @property
    def websocket_close_code(self) -> int:
        return websocket_close_code(self.status_code)


def websocket_close_code(status_code: int) -> int:
    """Application close codes mirror the HTTP status: 4401, 4403, 4404, ..."""
    return 4000 + status_code


_STATUS_CODES = {
    Refusal.UNAUTHENTICATED: 401,
    Refusal.NOT_INVITED: 403,
    Refusal.IDENTITY_UNAVAILABLE: 503,
    Refusal.CROSS_SITE: 403,
}
# A browser names the page that opens a WebSocket or sends one of these requests in Origin.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class Refused:
    reason: Refusal
    email: str | None = None  # the verified email, when there is one


Refuse = Callable[[HTTPConnection, Refused], Response]


class IdentityGuard:
    def __init__(
        self, app: ASGIApp, *, identity: Identity, accounts: Accounts, refuse: Refuse
    ) -> None:
        self._app = app
        self._identity = identity
        self._accounts = accounts
        self._refuse = refuse

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        conn = HTTPConnection(scope)
        admitted = await anyio.to_thread.run_sync(self._admit, conn)
        if isinstance(admitted, User):
            scope.setdefault("state", {})["user"] = admitted
            await self._app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            # Accept, then close with the reason: a socket refused during the handshake reaches
            # the browser as a bare 1006, indistinguishable from a network failure.
            await receive()  # websocket.connect
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close", "code": admitted.reason.websocket_close_code})
            return
        await self._refuse(conn, admitted)(scope, receive, send)

    def _admit(self, conn: HTTPConnection) -> User | Refused:
        if _from_another_site(conn):
            # The proxy's cookie would vouch for the person, but another site is acting for them.
            return Refused(Refusal.CROSS_SITE)
        try:
            email = self._identity.verified_email(conn)
        except IdentityUnavailable:
            return Refused(Refusal.IDENTITY_UNAVAILABLE)
        if email is None:
            return Refused(Refusal.UNAUTHENTICATED)
        return self._accounts.admit(email) or Refused(Refusal.NOT_INVITED, email)


def _from_another_site(conn: HTTPConnection) -> bool:
    if conn.scope["type"] == "http" and conn.scope["method"] not in UNSAFE_METHODS:
        return False
    origin = conn.headers.get("origin")
    if origin is None:
        return False  # not sent by a browser
    return urlsplit(origin).netloc != conn.headers.get("host")


def current_user(conn: HTTPConnection) -> User:
    user = conn.state.user
    assert isinstance(user, User), "IdentityGuard must run before any route"
    return user


CurrentUser = Annotated[User, Depends(current_user)]


class NotAdministrator(Exception):
    """Only an Administrator may do this."""


def current_administrator(user: CurrentUser) -> User:
    """Raises NotAdministrator."""
    if user.role != Role.ADMINISTRATOR:
        raise NotAdministrator
    return user


CurrentAdministrator = Annotated[User, Depends(current_administrator)]
