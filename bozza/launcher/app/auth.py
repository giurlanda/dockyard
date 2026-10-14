"""Verifica del JWT di Cloudflare Access su ogni richiesta HTTP e WebSocket.

Cloudflare Access aggiunge l'header `Cf-Access-Jwt-Assertion` (e il cookie
`CF_Authorization`) alle richieste che passano dal tunnel. Verificarlo qui
protegge anche se qualcuno raggiunge il container senza passare da Cloudflare.
"""
import asyncio

import jwt
from jwt import PyJWKClient
from starlette.requests import HTTPConnection
from starlette.responses import PlainTextResponse

from . import config

PUBLIC_PATHS = {"/healthz"}


class CloudflareAccessMiddleware:
    def __init__(self, app):
        self.app = app
        self.issuer = f"https://{config.CF_ACCESS_TEAM}.cloudflareaccess.com"
        self.jwks = None
        if not config.DEV_NO_AUTH:
            self.jwks = PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs",
                                    cache_keys=True, lifespan=3600)

    def _verify(self, token: str) -> str:
        key = self.jwks.get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"],
                            audience=config.CF_ACCESS_AUD, issuer=self.issuer)
        email = (claims.get("email") or "").lower()
        if email != config.ALLOWED_EMAIL:
            raise PermissionError("email non autorizzata")
        return email

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        if config.DEV_NO_AUTH or scope["path"] in PUBLIC_PATHS:
            return await self.app(scope, receive, send)

        conn = HTTPConnection(scope)
        token = conn.headers.get("cf-access-jwt-assertion") or conn.cookies.get("CF_Authorization")
        try:
            if not token:
                raise PermissionError("token assente")
            email = await asyncio.to_thread(self._verify, token)
        except Exception:
            if scope["type"] == "websocket":
                await receive()  # websocket.connect
                await send({"type": "websocket.close", "code": 4401})
                return
            return await PlainTextResponse("Forbidden", status_code=403)(scope, receive, send)

        scope.setdefault("state", {})["user_email"] = email
        await self.app(scope, receive, send)
