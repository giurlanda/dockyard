"""The Dockyard web application: pages and API, all behind the identity guard.

One Instance shares its database and domain objects among its listeners, one ASGI app for each
way in, each app with its own identity guard.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from fastapi import FastAPI, Form, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.responses import Response

from ..accounts import Accounts, Role
from ..clock import Clock, SystemClock
from ..database import Database
from ..git_credentials import GitCredentials
from ..identity import Identity, LocalSignInIdentity, SigningKeys, identity_for
from ..images import Images
from ..local_sign_in import LocalSignIn
from ..projects import Projects
from ..runtime import Runtime, RuntimeUnavailable
from ..settings import Settings
from ..tls import Certificate, local_certificate
from .all_projects import all_projects_routes
from .claude_account import claude_account_routes, claude_account_status
from .git_credentials import git_credentials_routes
from .guard import (
    ClientNetworks,
    CurrentUser,
    IdentityGuard,
    NotAdministrator,
    SignInPage,
    is_api,
)
from .i18n import LANGUAGE_COOKIE, Language
from .images import images_routes
from .local_sign_in import SIGN_IN, SIGN_OUT, local_sign_in_routes
from .pages import Pages, same_site_path
from .projects import projects_routes
from .users import users_routes

ONE_YEAR = 365 * 24 * 3600

log = logging.getLogger(__name__)


class ListenerName(StrEnum):
    """The ways into an Instance, each served on a listener of its own (ADR 0004)."""

    TUNNEL = "tunnel"  # reached by cloudflared, identity from Cloudflare Access
    LOCAL = "local"  # published on the host, over HTTPS, identity from Local sign-in


@dataclass(frozen=True)
class Listener:
    """One way into the Instance: its app and the address it is served on."""

    app: FastAPI
    host: str
    port: int
    certificate: Certificate | None = None  # served over HTTPS with it; else plain HTTP


@dataclass(frozen=True)
class Instance:
    """The domain objects, built once, and the listeners that share them."""

    clock: Clock
    local_sign_in: LocalSignIn
    listeners: Mapping[ListenerName, Listener]


def create_app(
    settings: Settings, runtime: Runtime, *, signing_keys: SigningKeys | None = None
) -> FastAPI:
    """The tunnel listener's app, for whoever serves only that one."""
    instance = create_instance(settings, runtime, signing_keys=signing_keys)
    return instance.listeners[ListenerName.TUNNEL].app


def create_instance(
    settings: Settings,
    runtime: Runtime,
    *,
    signing_keys: SigningKeys | None = None,
    clock: Clock | None = None,
    certificate: Certificate | None = None,
) -> Instance:
    """certificate: the local listener's, when already read; else read from the data volume."""
    clock = clock or SystemClock()
    database = Database(settings.data_dir / "dockyard.db")
    database.migrate()
    accounts = Accounts(database)
    local_sign_in = LocalSignIn(database, clock)
    accounts.bootstrap_first_administrator(settings.first_administrator_email)
    git_credentials = GitCredentials(database, settings.secret_key)
    projects = Projects(database, runtime, git_credentials, limits=settings.limits)
    images = Images(database, runtime)
    try:
        projects.resume()
    except RuntimeUnavailable as error:
        log.warning("Cannot pick up the clones and Projects in progress: %s", error)
    images.resume()
    upkeep = _Upkeep(projects, images)

    listeners: dict[ListenerName, Listener] = {}
    if settings.identity is not None:
        tunnel = _listener_app(
            identity_for(settings.identity, signing_keys),
            runtime,
            accounts,
            git_credentials,
            projects,
            images,
            upkeep,
        )
        listeners[ListenerName.TUNNEL] = Listener(
            tunnel, settings.listen_host, settings.listen_port
        )
    if settings.local is not None:
        local = _listener_app(
            LocalSignInIdentity(local_sign_in),
            runtime,
            accounts,
            git_credentials,
            projects,
            images,
            upkeep,
            # Docker's networks as they are at start: Docker gives a new one a range of its own.
            clients=ClientNetworks(admitted=settings.local.networks, refused=runtime.networks()),
            local_sign_in=local_sign_in,
        )
        listeners[ListenerName.LOCAL] = Listener(
            local,
            settings.listen_host,
            settings.local.port,
            certificate or local_certificate(settings.data_dir, settings.local),
        )
    return Instance(clock=clock, local_sign_in=local_sign_in, listeners=listeners)


class _Upkeep:
    """The Instance's background upkeep runs once, while any of its listeners is served."""

    def __init__(self, projects: Projects, images: Images) -> None:
        self._projects = projects
        self._images = images
        self._lock = threading.Lock()
        self._serving = 0

    @asynccontextmanager
    async def lifespan(self, app: FastAPI) -> AsyncIterator[None]:
        with self._lock:
            self._serving += 1
            if self._serving == 1:
                self._projects.keep_git_credentials()
                self._images.keep_current()
        try:
            yield
        finally:
            with self._lock:
                self._serving -= 1
                if self._serving == 0:
                    self._projects.close()
                    self._images.close()


def _listener_app(
    identity: Identity,
    runtime: Runtime,
    accounts: Accounts,
    git_credentials: GitCredentials,
    projects: Projects,
    images: Images,
    upkeep: _Upkeep,
    *,
    clients: ClientNetworks | None = None,
    local_sign_in: LocalSignIn | None = None,  # people sign in on this listener with it
) -> FastAPI:
    pages = Pages(local_sign_in=local_sign_in is not None)
    app = FastAPI(
        title="Dockyard",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=upkeep.lifespan,
    )
    app.add_middleware(
        IdentityGuard,
        identity=identity,
        accounts=accounts,
        refuse=pages.refusal,
        clients=clients,
        sign_in=SignInPage(SIGN_IN, public=(SIGN_IN, SIGN_OUT, "/language", "/static/"))
        if local_sign_in
        else None,
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
            failed_images=images.failed() if user.role == Role.ADMINISTRATOR else [],
        )

    @app.post("/language")
    def switch_language(
        lang: Annotated[Language, Form()], next: Annotated[str, Form()] = "/"
    ) -> RedirectResponse:
        response = RedirectResponse(same_site_path(next), status_code=303)
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
    if local_sign_in:
        app.include_router(local_sign_in_routes(local_sign_in, pages))
    app.mount("/static", StaticFiles(packages=[("dockyard.web", "static")]), name="static")
    return app
