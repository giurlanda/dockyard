"""The User's own page: who they are and, while Local sign-in is on, their password.

Every User reaches it, from either listener. Through the tunnel a password is never checked (ADR
0004): a User who came in through the authentication proxy sets their first password there, and
changes it on the local listener, by giving the current one.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from ..accounts import User
from ..identity import SESSION_COOKIE
from ..local_sign_in import (
    HasPassword,
    LocalSignIn,
    NotAUser,
    PasswordRefused,
    SignInRefused,
    SlowedDown,
)
from .guard import CurrentUser
from .local_sign_in import seconds_to_wait
from .pages import Pages

OWN_PAGE = "/me"


def own_page_routes(pages: Pages, local_sign_in: LocalSignIn | None, *, local: bool) -> APIRouter:
    """local_sign_in: None while Local sign-in is off. local: this is the local listener."""
    router = APIRouter()

    def own_page(
        request: Request,
        user: User,
        *,
        saved: bool = False,
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
            saved=saved,
            problem=problem,
            retry_in=retry_in,
        )
        if retry_in:
            page.headers["Retry-After"] = str(retry_in)
        return page

    @router.get(OWN_PAGE, response_class=HTMLResponse)
    def show(request: Request, user: CurrentUser, saved: bool = False) -> HTMLResponse:
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
                    # The local listener admits no request without a client address.
                    client=request.client.host if request.client else "",
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
        return RedirectResponse(f"{OWN_PAGE}?saved=true", status_code=303)

    return router
