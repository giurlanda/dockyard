"""Every HTTP request and WebSocket passes here: without a verified User, nothing gets through."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

import anyio
from fastapi import Depends
from starlette.requests import HTTPConnection
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from ..accounts import Accounts, User
from ..identity import Identity, IdentityUnavailable


class Refusal(StrEnum):
    UNAUTHENTICATED = "unauthenticated"
    NOT_INVITED = "not_invited"
    IDENTITY_UNAVAILABLE = "identity_unavailable"

    @property
    def status_code(self) -> int:
        return _STATUS_CODES[self]

    @property
    def websocket_close_code(self) -> int:
        """Application close codes mirror the HTTP status: 4401, 4403, ..."""
        return 4000 + self.status_code


_STATUS_CODES = {
    Refusal.UNAUTHENTICATED: 401,
    Refusal.NOT_INVITED: 403,
    Refusal.IDENTITY_UNAVAILABLE: 503,
}


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
        try:
            email = self._identity.verified_email(conn)
        except IdentityUnavailable:
            return Refused(Refusal.IDENTITY_UNAVAILABLE)
        if email is None:
            return Refused(Refusal.UNAUTHENTICATED)
        return self._accounts.find_user(email) or Refused(Refusal.NOT_INVITED, email)


def current_user(conn: HTTPConnection) -> User:
    user = conn.state.user
    assert isinstance(user, User), "IdentityGuard must run before any route"
    return user


CurrentUser = Annotated[User, Depends(current_user)]
