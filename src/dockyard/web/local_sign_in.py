"""Local sign-in's pages: signing in with email and password, then a code when the User has a
Second factor; signing out; and choosing a password with an Activation link, a first one or a
new one.

Only the local listener has them (ADR 0004).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from ..identity import SESSION_COOKIE
from ..local_sign_in import (
    CODE_STEP_LIFETIME,
    SESSION_LIFETIME,
    ActivationLink,
    ActivationLinkInvalid,
    ActivationLinkKind,
    CodeAsked,
    CodeStepOver,
    LocalSignIn,
    PasswordRefused,
    SignedIn,
    SignInRefused,
    SlowedDown,
)
from .guard import signed_in_user
from .pages import Pages, same_site_path

SIGN_IN = "/sign-in"
CODE_STEP = "/sign-in/code"  # after the password, for a User with a Second factor
# The code step the browser is at: like the session cookie, for this site's pages alone.
CODE_STEP_COOKIE = "__Host-dockyard_code_step"
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
        return self._url(self.local_sign_in.new_activation_link(email))

    def new_to_reset(self, email: str) -> str | None:
        """A new Activation link with which the User chooses a new password, voiding the one they
        had. None when there is no such User."""
        return self._url(self.local_sign_in.new_activation_link_to_reset(email))

    def _url(self, token: str | None) -> str | None:
        return f"{self.base_url}{ACTIVATE}{token}" if token else None


def local_sign_in_routes(local_sign_in: LocalSignIn, pages: Pages) -> APIRouter:
    router = APIRouter()

    def sign_in_page(
        request: Request,
        *,
        next: str,
        email: str = "",
        refused: SignInRefused | None = None,
        code_step_over: bool = False,
    ) -> HTMLResponse:
        return refusable_page(
            request,
            "local_sign_in.html",
            refused,
            status_code=401 if code_step_over else 200,
            next=next,
            email=email,
            code_step_over=code_step_over,
        )

    def code_page(
        request: Request, *, next: str, refused: SignInRefused | None = None
    ) -> HTMLResponse:
        return refusable_page(request, "sign_in_code.html", refused, next=next)

    def refusable_page(
        request: Request,
        template: str,
        refused: SignInRefused | None,
        *,
        next: str,
        status_code: int = 200,
        **context: object,
    ) -> HTMLResponse:
        """A page with a form that was refused, or slowed down, or neither yet."""
        retry_in = seconds_to_wait(refused) if refused else None
        slowed_down = isinstance(refused, SlowedDown)
        page = pages.render(
            request,
            template,
            status_code=429 if slowed_down else 401 if refused else status_code,
            next=same_site_path(next),
            wrong=refused is not None and not slowed_down,
            retry_in=retry_in,
            **context,
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
            outcome = local_sign_in.sign_in(email, password, client)
        except SignInRefused as refused:
            return sign_in_page(request, next=next, email=email, refused=refused)
        return _onward(outcome, next)

    @router.get(CODE_STEP, response_model=None)
    def code_form(request: Request, next: str = "/") -> Response:
        if signed_in_user(request) is not None:
            return RedirectResponse(same_site_path(next), status_code=303)
        step = request.cookies.get(CODE_STEP_COOKIE)
        if not step or local_sign_in.code_step(step) is None:
            return RedirectResponse(_with_next(SIGN_IN, next), status_code=303)
        return code_page(request, next=next)

    @router.post(CODE_STEP, response_model=None)
    def give_code(
        request: Request,
        code: Annotated[str, Form()] = "",
        next: Annotated[str, Form()] = "/",
    ) -> Response:
        client = request.client.host if request.client else ""
        try:
            session = local_sign_in.complete_sign_in(
                request.cookies.get(CODE_STEP_COOKIE, ""), code, client
            )
        except CodeStepOver:
            page = sign_in_page(request, next=next, code_step_over=True)
            page.delete_cookie(CODE_STEP_COOKIE, path="/", secure=True, httponly=True)
            return page
        except SignInRefused as refused:
            return code_page(request, next=next, refused=refused)
        return _onward(SignedIn(session), next)

    @router.post(SIGN_OUT)
    def sign_out(request: Request) -> RedirectResponse:
        if token := request.cookies.get(SESSION_COOKIE):
            local_sign_in.sign_out(token)
        return _with_session(RedirectResponse(SIGN_IN, status_code=303), None)

    def link_invalid(request: Request) -> HTMLResponse:
        return pages.notice(request, "activation_link_invalid", status_code=410)

    def activation_page(
        request: Request,
        link: ActivationLink,
        *,
        problem: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        page = pages.render(
            request,
            "activate.html",
            status_code=status_code,
            email=link.email,
            reset=link.kind == ActivationLinkKind.RESET,
            problem=problem,
        )
        # The link is in the page's address: never told to another site. Not no-referrer, which
        # would send the form with Origin: null, refused as another site's.
        page.headers["Referrer-Policy"] = "same-origin"
        return page

    @router.get(ACTIVATE + "{token}", response_class=HTMLResponse)
    def activation_form(request: Request, token: str) -> HTMLResponse:
        link = local_sign_in.activation_link(token)
        if link is None:
            return link_invalid(request)
        return activation_page(request, link)

    @router.post(ACTIVATE + "{token}", response_model=None)
    def activate(
        request: Request,
        token: str,
        password: Annotated[str, Form()] = "",
        repeated: Annotated[str, Form()] = "",
    ) -> Response:
        link = local_sign_in.activation_link(token)
        if link is None:
            return link_invalid(request)
        if password != repeated:
            return activation_page(request, link, problem="different", status_code=422)
        try:
            outcome = local_sign_in.activate(token, password)
        except PasswordRefused as refused:
            return activation_page(request, link, problem=refused.rule, status_code=422)
        except ActivationLinkInvalid:
            return link_invalid(request)
        return _onward(outcome, "/")

    return router


def seconds_to_wait(refused: SignInRefused) -> int | None:
    """Whole seconds, rounded up: trying again when told never comes too early."""
    return math.ceil(refused.retry_in.total_seconds()) if refused.retry_in else None


def _onward(outcome: SignedIn | CodeAsked, next: str) -> RedirectResponse:
    """Signed in: to the page asked for. Asked for a code: to the code step first."""
    if isinstance(outcome, CodeAsked):
        response = RedirectResponse(_with_next(CODE_STEP, next), status_code=303)
        response.set_cookie(
            CODE_STEP_COOKIE,
            outcome.step,
            max_age=int(CODE_STEP_LIFETIME / timedelta(seconds=1)),
            path="/",
            secure=True,
            httponly=True,
            samesite="strict",
        )
        return response
    response = _with_session(
        RedirectResponse(same_site_path(next), status_code=303), outcome.session
    )
    response.delete_cookie(CODE_STEP_COOKIE, path="/", secure=True, httponly=True)
    return response


def _with_next(path: str, next: str) -> str:
    next = same_site_path(next)
    return path if next == "/" else f"{path}?next={quote(next, safe='')}"


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
