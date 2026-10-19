"""Identity: turns a request (HTTP or WebSocket) into a verified email, or nothing."""

from __future__ import annotations

import logging
from typing import Protocol

import jwt
from starlette.requests import HTTPConnection

from .local_sign_in import LocalSignIn
from .settings import CloudflareAccess, DevelopmentHeader, IdentitySource

log = logging.getLogger(__name__)

DEVELOPMENT_HEADER = "X-Dockyard-Dev-Email"
# __Host-: the browser keeps it only from a secure page, for this host and every path of it.
SESSION_COOKIE = "__Host-dockyard_session"


class IdentityUnavailable(Exception):
    """Identities cannot be verified right now (for example, the proxy's keys are unreachable)."""


class Identity(Protocol):
    def verified_email(self, conn: HTTPConnection) -> str | None:
        """The email the request proves, or None. Raises IdentityUnavailable."""
        ...


class SigningKeys(Protocol):
    """Where the proxy publishes the keys that sign its tokens."""

    def get_signing_key_from_jwt(self, token: str) -> jwt.PyJWK: ...


class CloudflareAccessIdentity:
    """Trusts only tokens signed by the team's Cloudflare Access for this application."""

    def __init__(self, access: CloudflareAccess, signing_keys: SigningKeys | None = None) -> None:
        self._issuer = access.issuer
        self._audience = access.audience
        self._signing_keys = signing_keys or jwt.PyJWKClient(
            f"{access.issuer}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600
        )

    def verified_email(self, conn: HTTPConnection) -> str | None:
        token = conn.headers.get("cf-access-jwt-assertion") or conn.cookies.get("CF_Authorization")
        if not token:
            return None
        try:
            key = self._signing_keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWKClientConnectionError as error:
            log.warning("Cannot fetch the Cloudflare Access signing keys: %s", error)
            raise IdentityUnavailable from error
        except jwt.PyJWTError:
            return None
        email = claims.get("email")
        return email.strip() or None if isinstance(email, str) else None


class DevelopmentHeaderIdentity:
    def verified_email(self, conn: HTTPConnection) -> str | None:
        return conn.headers.get(DEVELOPMENT_HEADER, "").strip() or None


class LocalSignInIdentity:
    """Whoever signed in with Local sign-in. Never a Cloudflare Access token: the local listener
    ignores them."""

    def __init__(self, local_sign_in: LocalSignIn) -> None:
        self._local_sign_in = local_sign_in

    def verified_email(self, conn: HTTPConnection) -> str | None:
        token = conn.cookies.get(SESSION_COOKIE)
        return self._local_sign_in.signed_in_email(token) if token else None


def identity_for(source: IdentitySource, signing_keys: SigningKeys | None = None) -> Identity:
    match source:
        case CloudflareAccess():
            return CloudflareAccessIdentity(source, signing_keys)
        case DevelopmentHeader():
            log.warning(
                "Development identity enabled: anyone who can reach Dockyard can claim any email "
                "with the %s header. Never enable it on a reachable Instance.",
                DEVELOPMENT_HEADER,
            )
            return DevelopmentHeaderIdentity()
