"""The Dockyard web application: pages and API, all behind the identity guard."""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, Form, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.exceptions import HTTPException
from starlette.responses import Response

from ..accounts import Accounts
from ..database import Database
from ..identity import SigningKeys, identity_for
from ..settings import Settings
from .guard import CurrentUser, IdentityGuard
from .i18n import LANGUAGE_COOKIE, Language
from .pages import Pages

ONE_YEAR = 365 * 24 * 3600


def create_app(settings: Settings, *, signing_keys: SigningKeys | None = None) -> FastAPI:
    database = Database(settings.data_dir / "dockyard.db")
    database.migrate()
    accounts = Accounts(database)
    accounts.bootstrap_first_administrator(settings.first_administrator_email)
    pages = Pages()

    app = FastAPI(title="Dockyard", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        IdentityGuard,
        identity=identity_for(settings.identity, signing_keys),
        accounts=accounts,
        refuse=pages.refusal,
    )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> Response:
        if error.status_code == 404 and not request.url.path.startswith("/api/"):
            return pages.notice(request, "not_found", status_code=404, home=True)
        return await http_exception_handler(request, error)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, user: CurrentUser) -> HTMLResponse:
        return pages.render(request, "dashboard.html", user=user)

    @app.post("/language")
    def switch_language(
        lang: Annotated[Language, Form()], next: Annotated[str, Form()] = "/"
    ) -> RedirectResponse:
        response = RedirectResponse(_local_path(next), status_code=303)
        response.set_cookie(LANGUAGE_COOKIE, lang, max_age=ONE_YEAR, httponly=True, samesite="lax")
        return response

    @app.get("/api/me")
    def me(user: CurrentUser) -> dict[str, str]:
        return {"email": user.email, "role": user.role}

    return app


def _local_path(target: str) -> str:
    """Only paths on this site: never an open redirect."""
    if target.startswith("/") and not target.startswith(("//", "/\\")):
        return target
    return "/"
