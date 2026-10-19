"""Local sign-in's pages: signing in with email and password, signing out, and choosing a
password with an Activation link.

Only the local listener has them (ADR 0004).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from ..identity import SESSION_COOKIE
from ..local_sign_in import (
    SESSION_LIFETIME,
    ActivationLinkInvalid,
    LocalSignIn,
    PasswordRefused,
    SignInRefused,
    SlowedDown,
)
from .guard import signed_in_user
from .pages import Pages, same_site_path

SIGN_IN = "/sign-in"
SIGN_OUT = "/sign-out"  # reached without a session too: one already over is still forgotten
ACTIVATE = "/activate/"  # followed by the Activation link's token


@dataclass(frozen=True)
class ActivationLinks:
    """Makes Activation links, on any listener, that point to the local one."""

    local_sign_in: LocalSignIn
    base_url: str  # where people open Local sign-in

    def new(self, email: str) -> str | None:
        """A new Activation link for the person's pending Invitation, voiding the one they had.
        None when there is no such Invitation."""
        token = self.local_sign_in.new_activation_link(email)
        return f"{self.base_url}{ACTIVATE}{token}" if token else None


def local_sign_in_routes(local_sign_in: LocalSignIn, pages: Pages) -> APIRouter:
    router = APIRouter()

    def sign_in_page(
        request: Request, *, next: str, email: str = "", refused: SignInRefused | None = None
    ) -> HTMLResponse:
        # Whole seconds, rounded up: trying again when told never comes too early.
        retry_in = None
        if refused and refused.retry_in:
            retry_in = math.ceil(refused.retry_in.total_seconds())
        slowed_down = isinstance(refused, SlowedDown)
        page = pages.render(
            request,
            "local_sign_in.html",
            status_code=429 if slowed_down else 401 if refused else 200,
            next=same_site_path(next),
            email=email,
            wrong=refused is not None and not slowed_down,
            retry_in=retry_in,
        )
        if retry_in:
            page.headers["Retry-After"] = str(retry_in)
        return page

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
        # The local listener admits no request without a client address.
        client = request.client.host if request.client else ""
        try:
            token = local_sign_in.sign_in(email, password, client)
        except SignInRefused as refused:
            return sign_in_page(request, next=next, email=email, refused=refused)
        return _with_session(RedirectResponse(same_site_path(next), status_code=303), token)

    @router.post(SIGN_OUT)
    def sign_out(request: Request) -> RedirectResponse:
        if token := request.cookies.get(SESSION_COOKIE):
            local_sign_in.sign_out(token)
        return _with_session(RedirectResponse(SIGN_IN, status_code=303), None)

    def link_invalid(request: Request) -> HTMLResponse:
        return pages.notice(request, "activation_link_invalid", status_code=410)

    def activation_page(
        request: Request, email: str, *, problem: str | None = None, status_code: int = 200
    ) -> HTMLResponse:
        page = pages.render(
            request, "activate.html", status_code=status_code, email=email, problem=problem
        )
        # The link is in the page's address: never told to another site. Not no-referrer, which
        # would send the form with Origin: null, refused as another site's.
        page.headers["Referrer-Policy"] = "same-origin"
        return page

    @router.get(ACTIVATE + "{token}", response_class=HTMLResponse)
    def activation_form(request: Request, token: str) -> HTMLResponse:
        email = local_sign_in.activation_link_email(token)
        if email is None:
            return link_invalid(request)
        return activation_page(request, email)

    @router.post(ACTIVATE + "{token}", response_model=None)
    def activate(
        request: Request,
        token: str,
        password: Annotated[str, Form()] = "",
        repeated: Annotated[str, Form()] = "",
    ) -> Response:
        email = local_sign_in.activation_link_email(token)
        if email is None:
            return link_invalid(request)
        if password != repeated:
            return activation_page(request, email, problem="different", status_code=422)
        try:
            session = local_sign_in.activate(token, password)
        except PasswordRefused as refused:
            return activation_page(request, email, problem=refused.rule, status_code=422)
        except ActivationLinkInvalid:
            return link_invalid(request)
        return _with_session(RedirectResponse("/", status_code=303), session)

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
