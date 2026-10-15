"""Claude Account: whether a User's is connected, and the login environment that connects it."""

from __future__ import annotations

import logging
from http import HTTPStatus

import anyio
from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from starlette.responses import Response

from ..accounts import User
from ..projects import Projects
from ..runtime import LoginNotRunning, Runtime, RuntimeUnavailable
from .guard import CurrentUser, websocket_close_code
from .pages import Pages
from .terminal import bridge

log = logging.getLogger(__name__)


class ClaudeAccountStatus(BaseModel):
    connected: bool
    login_in_progress: bool


def claude_account_status(runtime: Runtime, user: User) -> ClaudeAccountStatus:
    return ClaudeAccountStatus(
        connected=runtime.claude_account_connected(user.runtime_id),
        login_in_progress=runtime.login_environment_running(user.runtime_id),
    )


def claude_account_routes(runtime: Runtime, projects: Projects, pages: Pages) -> APIRouter:
    router = APIRouter()

    def start_login(user: User) -> None:
        runtime.start_login_environment(user.runtime_id)
        projects.follow_sign_in(user)  # Projects waiting for the Claude Account start after it

    @router.post("/claude-account/login")
    def connect(user: CurrentUser) -> RedirectResponse:
        start_login(user)
        return RedirectResponse("/claude-account/login", status_code=303)

    @router.get("/claude-account/login", response_class=HTMLResponse, response_model=None)
    def login_page(request: Request, user: CurrentUser) -> Response:
        if not runtime.login_environment_running(user.runtime_id):
            return RedirectResponse("/", status_code=303)
        return pages.render(request, "sign_in.html", user=user)

    @router.post("/claude-account/login/cancel")
    def cancel(user: CurrentUser) -> RedirectResponse:
        runtime.stop_login_environment(user.runtime_id)
        return RedirectResponse("/", status_code=303)

    @router.get("/api/claude-account")
    def status(user: CurrentUser) -> ClaudeAccountStatus:
        return claude_account_status(runtime, user)

    @router.post("/api/claude-account/login", status_code=202)
    def start_login_api(user: CurrentUser) -> ClaudeAccountStatus:
        start_login(user)
        return claude_account_status(runtime, user)

    @router.delete("/api/claude-account/login", status_code=204)
    def cancel_login(user: CurrentUser) -> None:
        runtime.stop_login_environment(user.runtime_id)

    @router.websocket("/ws/claude-account/login")
    async def login_terminal(websocket: WebSocket, user: CurrentUser) -> None:
        await websocket.accept()
        try:
            tty = await anyio.to_thread.run_sync(runtime.attach_login_environment, user.runtime_id)
        except LoginNotRunning:
            await websocket.close(websocket_close_code(HTTPStatus.NOT_FOUND))
            return
        except RuntimeUnavailable as error:
            log.warning("The Runtime is unavailable: %s", error)
            await websocket.close(websocket_close_code(HTTPStatus.SERVICE_UNAVAILABLE))
            return
        await bridge(websocket, tty)

    return router
