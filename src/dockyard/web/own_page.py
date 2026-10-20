"""The User's own page: who they are and, while Local sign-in is on, their password and their
Second factor.

Every User reaches it, from either listener. Through the tunnel a password is never checked (ADR
0004): a User who came in through the authentication proxy sets their first password there, and
changes it on the local listener, by giving the current one. The Second factor, a part of Local
sign-in alone, is turned on and off on the local listener only.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated
from urllib.parse import quote

import segno
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import Markup
from starlette.responses import Response

from ..accounts import User
from ..identity import SESSION_COOKIE
from ..local_sign_in import (
    CodeWrong,
    HasPassword,
    HasSecondFactor,
    LocalSignIn,
    NoSecondFactor,
    NotAUser,
    NothingToConfirm,
    PasswordRefused,
    SignInRefused,
    SlowedDown,
)
from .guard import CurrentUser
from .local_sign_in import seconds_to_wait
from .pages import Pages

OWN_PAGE = "/me"
SECOND_FACTOR = OWN_PAGE + "/second-factor"
ISSUER = "Dockyard"  # how the authenticator app names the codes, with the email


def own_page_routes(pages: Pages, local_sign_in: LocalSignIn | None, *, local: bool) -> APIRouter:
    """local_sign_in: None while Local sign-in is off. local: this is the local listener."""
    router = APIRouter()

    def own_page(
        request: Request,
        user: User,
        *,
        saved: str | None = None,  # what was just saved: "password", "second_factor_off"
        about: str = "password",  # the section the problem or the slowdown is about
        problem: str | None = None,  # the message's key
        retry_in: int | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        page = pages.render(
            request,
            "own_page.html",
            status_code=status_code,
            user=user,
            passwords=local_sign_in is not None,
            has_password=local_sign_in is not None and local_sign_in.has_password(user.email),
            recovery_codes_left=local_sign_in and local_sign_in.recovery_codes_left(user.email),
            saved=saved,
            about=about,
            problem=problem,
            retry_in=retry_in,
        )
        if retry_in:
            page.headers["Retry-After"] = str(retry_in)
        return page

    @router.get(OWN_PAGE, response_class=HTMLResponse)
    def show(request: Request, user: CurrentUser, saved: str | None = None) -> HTMLResponse:
        return own_page(request, user, saved=saved)

    @router.post(OWN_PAGE + "/password", response_model=None)
    def set_password(
        request: Request,
        user: CurrentUser,
        current: Annotated[str, Form()] = "",
        password: Annotated[str, Form()] = "",
        repeated: Annotated[str, Form()] = "",
    ) -> Response:
        if local_sign_in is None:
            raise HTTPException(404)
        if password != repeated:
            problem = "activate.refused.different"
            return own_page(request, user, problem=problem, status_code=422)
        try:
            if local and local_sign_in.has_password(user.email):
                local_sign_in.change_password(
                    user.email,
                    current,
                    password,
                    client=_client(request),
                    keep=request.cookies.get(SESSION_COOKIE),
                )
            else:
                local_sign_in.set_first_password(user.email, password)
        except SlowedDown as slowed_down:
            retry_in = seconds_to_wait(slowed_down)
            return own_page(request, user, retry_in=retry_in, status_code=429)
        except SignInRefused:
            problem = "own_page.password.wrong_current"
            return own_page(request, user, problem=problem, status_code=403)
        except PasswordRefused as refused:
            problem = f"activate.refused.{refused.rule}"
            return own_page(request, user, problem=problem, status_code=422)
        except HasPassword:  # through the tunnel, or set meanwhile
            problem = "own_page.password.already_set"
            return own_page(request, user, problem=problem, status_code=409)
        except NotAUser:  # removed meanwhile
            raise HTTPException(404) from None
        return RedirectResponse(f"{OWN_PAGE}?saved=password", status_code=303)

    if local and local_sign_in is not None:
        router.include_router(_second_factor_routes(pages, local_sign_in, own_page))
    return router


def _second_factor_routes(
    pages: Pages, local_sign_in: LocalSignIn, own_page: Callable[..., HTMLResponse]
) -> APIRouter:
    router = APIRouter()

    def enrolment_page(
        request: Request, user: User, secret: str, *, wrong: bool = False
    ) -> HTMLResponse:
        uri = f"otpauth://totp/{quote(f'{ISSUER}:{user.email}', safe=':@')}"
        uri += f"?secret={secret}&issuer={ISSUER}"
        page = pages.render(
            request,
            "second_factor.html",
            status_code=422 if wrong else 200,
            user=user,
            qr_code=_qr_code(uri),
            secret=" ".join(secret[i : i + 4] for i in range(0, len(secret), 4)),
            wrong=wrong,
        )
        return _not_kept(page)

    def recovery_codes_page(
        request: Request, user: User, codes: list[str], *, turned_on: bool
    ) -> HTMLResponse:
        page = pages.render(
            request, "recovery_codes.html", user=user, codes=codes, turned_on=turned_on
        )
        return _not_kept(page)

    def refused(request: Request, user: User, refusal: SignInRefused, wrong: str) -> HTMLResponse:
        """The User's page, telling why what they asked of their Second factor was refused."""
        if isinstance(refusal, SlowedDown):
            retry_in = seconds_to_wait(refusal)
            return own_page(
                request, user, about="second_factor", retry_in=retry_in, status_code=429
            )
        problem = f"own_page.second_factor.{wrong}"
        return own_page(request, user, about="second_factor", problem=problem, status_code=403)

    @router.post(SECOND_FACTOR, response_model=None)
    def start(
        request: Request, user: CurrentUser, password: Annotated[str, Form()] = ""
    ) -> Response:
        try:
            local_sign_in.start_second_factor(user.email, password, client=_client(request))
        except SignInRefused as refusal:
            return refused(request, user, refusal, "wrong_password")
        except HasSecondFactor:
            return _back()
        return RedirectResponse(SECOND_FACTOR, status_code=303)

    @router.get(SECOND_FACTOR, response_model=None)
    def enrolment(request: Request, user: CurrentUser) -> Response:
        secret = local_sign_in.pending_second_factor(user.email)
        if secret is None:
            return _back()
        return enrolment_page(request, user, secret)

    @router.post(SECOND_FACTOR + "/confirm", response_model=None)
    def confirm(request: Request, user: CurrentUser, code: Annotated[str, Form()] = "") -> Response:
        try:
            codes = local_sign_in.confirm_second_factor(user.email, code)
        except CodeWrong:
            secret = local_sign_in.pending_second_factor(user.email)
            return _back() if secret is None else enrolment_page(request, user, secret, wrong=True)
        except NothingToConfirm:
            return _back()
        return recovery_codes_page(request, user, codes, turned_on=True)

    @router.post(SECOND_FACTOR + "/recovery-codes", response_model=None)
    def new_recovery_codes(
        request: Request,
        user: CurrentUser,
        code_or_password: Annotated[str, Form()] = "",
    ) -> Response:
        try:
            codes = local_sign_in.new_recovery_codes(
                user.email, code_or_password, client=_client(request)
            )
        except SignInRefused as refusal:
            return refused(request, user, refusal, "wrong")
        except NoSecondFactor:
            return _back()
        return recovery_codes_page(request, user, codes, turned_on=False)

    @router.post(SECOND_FACTOR + "/off", response_model=None)
    def turn_off(
        request: Request,
        user: CurrentUser,
        code_or_password: Annotated[str, Form()] = "",
    ) -> Response:
        try:
            local_sign_in.turn_off_second_factor(
                user.email, code_or_password, client=_client(request)
            )
        except SignInRefused as refusal:
            return refused(request, user, refusal, "wrong")
        except NoSecondFactor:
            return _back()
        return RedirectResponse(f"{OWN_PAGE}?saved=second_factor_off", status_code=303)

    return router


def _qr_code(uri: str) -> Markup:
    """The QR code an authenticator app scans, as SVG that scales with the page: dark on light
    whatever the theme, as apps expect."""
    svg = segno.make(uri, error="m").svg_inline(
        border=4, dark="#000", light="#fff", omitsize=True, svgclass="qr-code"
    )
    return Markup(svg)  # made here, from nothing but the URI


def _back() -> RedirectResponse:
    """To the User's page: nothing (left) to do here."""
    return RedirectResponse(OWN_PAGE, status_code=303)


def _not_kept(page: HTMLResponse) -> HTMLResponse:
    """A page with a secret on it: never kept by the browser, nor anything in between."""
    page.headers["Cache-Control"] = "no-store"
    return page


def _client(request: Request) -> str:
    # The local listener admits no request without a client address.
    return request.client.host if request.client else ""
