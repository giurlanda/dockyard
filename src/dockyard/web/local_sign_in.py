"""Local sign-in's pages: signing in with email and password, and signing out.

Only the local listener has them (ADR 0004).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from ..identity import SESSION_COOKIE
from ..local_sign_in import SESSION_LIFETIME, LocalSignIn
from .guard import signed_in_user
from .pages import Pages, same_site_path

SIGN_IN = "/sign-in"
SIGN_OUT = "/sign-out"  # reached without a session too: one already over is still forgotten


def local_sign_in_routes(local_sign_in: LocalSignIn, pages: Pages) -> APIRouter:
    router = APIRouter()

    def sign_in_page(
        request: Request, *, next: str, email: str = "", refused: bool = False
    ) -> HTMLResponse:
        return pages.render(
            request,
            "local_sign_in.html",
            status_code=401 if refused else 200,
            next=same_site_path(next),
            email=email,
            refused=refused,
        )

    @router.get(SIGN_IN, response_model=None)
    def sign_in_form(request: Request, next: str = "/") -> Response:
        if signed_in_user(request) is not None:
            return RedirectResponse(same_site_path(next), status_code=303)
        return sign_in_page(request, next=next)

    @router.post(SIGN_IN, response_model=None)
    def sign_in(
        request: Request,
        email: Annotated[str, Form()] = "",
        password: Annotated[str, Form()] = "",
        next: Annotated[str, Form()] = "/",
    ) -> Response:
        token = local_sign_in.sign_in(email, password)
        if token is None:
            return sign_in_page(request, next=next, email=email, refused=True)
        return _with_session(RedirectResponse(same_site_path(next), status_code=303), token)

    @router.post(SIGN_OUT)
    def sign_out(request: Request) -> RedirectResponse:
        if token := request.cookies.get(SESSION_COOKIE):
            local_sign_in.sign_out(token)
        return _with_session(RedirectResponse(SIGN_IN, status_code=303), None)

    return router


def _with_session(response: RedirectResponse, token: str | None) -> RedirectResponse:
    """The browser keeps the session (None: forgets it) for this site's pages alone, out of reach
    of scripts and never over plain HTTP."""
    response.set_cookie(
        SESSION_COOKIE,
        token or "",
        max_age=int(SESSION_LIFETIME / timedelta(seconds=1)) if token else 0,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return response
