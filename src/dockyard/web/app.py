"""The Dockyard web application: pages and API, all behind the identity guard."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import FastAPI, Form, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.responses import Response

from ..accounts import Accounts
from ..database import Database
from ..identity import SigningKeys, identity_for
from ..projects import Projects
from ..runtime import Runtime, RuntimeUnavailable
from ..settings import Settings
from .claude_account import claude_account_routes, claude_account_status
from .guard import CurrentUser, IdentityGuard
from .i18n import LANGUAGE_COOKIE, Language
from .pages import Pages, is_api
from .projects import projects_routes

ONE_YEAR = 365 * 24 * 3600

log = logging.getLogger(__name__)


def create_app(
    settings: Settings, runtime: Runtime, *, signing_keys: SigningKeys | None = None
) -> FastAPI:
    database = Database(settings.data_dir / "dockyard.db")
    database.migrate()
    accounts = Accounts(database)
    accounts.bootstrap_first_administrator(settings.first_administrator_email)
    projects = Projects(database, runtime)
    try:
        projects.resume_clones()
    except RuntimeUnavailable as error:
        log.warning("Cannot follow the clones in progress: %s", error)
    pages = Pages()

    app = FastAPI(title="Dockyard", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        IdentityGuard,
        identity=identity_for(settings.identity, signing_keys),
        accounts=accounts,
        refuse=pages.refusal,
    )

    @app.exception_handler(RuntimeUnavailable)
    async def runtime_unavailable(request: Request, error: RuntimeUnavailable) -> Response:
        log.warning("The Runtime is unavailable: %s", error)
        error_name = "runtime_unavailable"
        return pages.problem(request, error_name, key=error_name, status_code=503)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> Response:
        if error.status_code == 404 and not is_api(request):
            return pages.notice(request, "not_found", status_code=404, home=True)
        return await http_exception_handler(request, error)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, user: CurrentUser) -> HTMLResponse:
        return pages.render(
            request,
            "dashboard.html",
            user=user,
            claude_account=claude_account_status(runtime, user),
            projects=projects.list(user),
        )

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

    app.include_router(claude_account_routes(runtime, pages))
    app.include_router(projects_routes(projects, pages))
    app.mount("/static", StaticFiles(packages=[("dockyard.web", "static")]), name="static")
    return app


def _local_path(target: str) -> str:
    """Only paths on this site: never an open redirect."""
    if target.startswith("/") and not target.startswith(("//", "/\\")):
        return target
    return "/"
