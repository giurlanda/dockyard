"""The Dockyard web application: pages and API, all behind the identity guard."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Form, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.responses import Response

from ..accounts import Accounts
from ..database import Database
from ..git_credentials import GitCredentials
from ..identity import SigningKeys, identity_for
from ..images import Images
from ..projects import Projects
from ..runtime import Runtime, RuntimeUnavailable
from ..settings import Settings
from .all_projects import all_projects_routes
from .claude_account import claude_account_routes, claude_account_status
from .git_credentials import git_credentials_routes
from .guard import CurrentUser, IdentityGuard, NotAdministrator
from .i18n import LANGUAGE_COOKIE, Language
from .images import images_routes
from .pages import Pages, is_api
from .projects import projects_routes
from .users import users_routes

ONE_YEAR = 365 * 24 * 3600

log = logging.getLogger(__name__)


def create_app(
    settings: Settings, runtime: Runtime, *, signing_keys: SigningKeys | None = None
) -> FastAPI:
    database = Database(settings.data_dir / "dockyard.db")
    database.migrate()
    accounts = Accounts(database)
    accounts.bootstrap_first_administrator(settings.first_administrator_email)
    git_credentials = GitCredentials(database, settings.secret_key)
    projects = Projects(database, runtime, git_credentials, limits=settings.limits)
    images = Images(database, runtime)
    try:
        projects.resume()
    except RuntimeUnavailable as error:
        log.warning("Cannot pick up the clones and Projects in progress: %s", error)
    images.resume()
    pages = Pages()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        projects.keep_git_credentials()
        yield
        projects.close()

    app = FastAPI(
        title="Dockyard", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
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

    @app.exception_handler(NotAdministrator)
    async def not_administrator(request: Request, error: NotAdministrator) -> Response:
        error_name = "administrator_only"
        return pages.problem(request, error_name, key=error_name, status_code=403)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> Response:
        # What was wrong, never what was sent: it may hold a Git Credential's token.
        detail = [{k: v for k, v in e.items() if k != "input"} for e in error.errors()]
        return JSONResponse({"detail": jsonable_encoder(detail)}, status_code=422)

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
            git_credentials=git_credentials.list(user),
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

    app.include_router(claude_account_routes(runtime, projects, pages))
    app.include_router(projects_routes(projects, git_credentials, images, pages))
    app.include_router(git_credentials_routes(git_credentials, pages))
    app.include_router(users_routes(accounts, projects, runtime, pages))
    app.include_router(all_projects_routes(projects, pages))
    app.include_router(images_routes(images, pages))
    app.mount("/static", StaticFiles(packages=[("dockyard.web", "static")]), name="static")
    return app


def _local_path(target: str) -> str:
    """Only paths on this site: never an open redirect."""
    if target.startswith("/") and not target.startswith(("//", "/\\")):
        return target
    return "/"
