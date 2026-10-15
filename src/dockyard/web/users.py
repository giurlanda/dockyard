"""Users and Invitations: an Administrator invites people, and manages the Users they become.

Every route here is for Administrators only.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from starlette.responses import Response

from ..accounts import (
    Accounts,
    Invitation,
    InvitationRefused,
    LastAdministrator,
    Refusal,
    Role,
    User,
)
from ..projects import Projects
from ..runtime import Runtime
from .guard import CurrentAdministrator
from .pages import Pages

# Refusals of what conflicts with how things are, rather than with what was asked.
_CONFLICTS = {Refusal.ALREADY_USER, Refusal.ALREADY_INVITED}


class NewInvitation(BaseModel):
    email: str


class InvitationOut(BaseModel):
    email: str
    created_at: str

    @classmethod
    def of(cls, invitation: Invitation) -> InvitationOut:
        return cls(email=invitation.email, created_at=invitation.created_at)


class RoleChange(BaseModel):
    role: Role


class UserOut(BaseModel):
    email: str
    role: Role

    @classmethod
    def of(cls, user: User) -> UserOut:
        return cls(email=user.email, role=user.role)


def users_routes(
    accounts: Accounts, projects: Projects, runtime: Runtime, pages: Pages
) -> APIRouter:
    router = APIRouter()

    def remove(email: str) -> bool:
        """Remove the User and everything they own. False if there is no such User.

        Raises LastAdministrator, RuntimeUnavailable.
        """
        runtime.check_reachable()  # nothing is removed while Docker cannot remove it all
        removed = accounts.remove_user(email)
        if removed is None:
            return False
        projects.remove_owner(removed)
        return True

    def refused(request: Request, refusal: InvitationRefused) -> Response:
        return pages.problem(
            request,
            refusal.reason,
            key=f"invitation_refused.{refusal.reason}",
            status_code=_status_code(refusal),
        )

    def last_administrator(request: Request) -> Response:
        error = "last_administrator"
        return pages.problem(request, error, key=error, status_code=409)

    def users_page(
        request: Request,
        user: User,
        *,
        email: str = "",
        problem: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        return pages.render(
            request,
            "users.html",
            status_code=status_code,
            user=user,
            users=accounts.users(),
            invitations=accounts.invitations(),
            email=email,
            problem=problem,
        )

    def user_page(
        request: Request,
        user: User,
        shown: User,
        *,
        changed: Role | None = None,
        problem: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        return pages.render(
            request,
            "user.html",
            status_code=status_code,
            user=user,
            shown=shown,
            projects=projects.list(shown),
            changed=changed,
            problem=problem,
        )

    def last_administrator_page(request: Request, user: User, shown: User) -> HTMLResponse:
        problem = "last_administrator.body"
        return user_page(request, user, shown, problem=problem, status_code=409)

    def found(email: str) -> User:
        shown = accounts.find_user(email)
        if shown is None:
            raise HTTPException(404)
        return shown

    @router.get("/api/users")
    def list_users(_: CurrentAdministrator) -> list[UserOut]:
        return [UserOut.of(user) for user in accounts.users()]

    @router.patch("/api/users/{email}", response_model=UserOut)
    def change_role(
        request: Request, _: CurrentAdministrator, email: str, change: RoleChange
    ) -> object:
        try:
            changed = accounts.change_role(email, change.role)
        except LastAdministrator:
            return last_administrator(request)
        if changed is None:
            raise HTTPException(404)
        return UserOut.of(changed)

    @router.delete("/api/users/{email}", status_code=204)
    def remove_user(request: Request, _: CurrentAdministrator, email: str) -> Response:
        try:
            removed = remove(email)
        except LastAdministrator:
            return last_administrator(request)
        if not removed:
            raise HTTPException(404)
        return Response(status_code=204)

    @router.get("/api/invitations")
    def list_invitations(_: CurrentAdministrator) -> list[InvitationOut]:
        return [InvitationOut.of(invitation) for invitation in accounts.invitations()]

    @router.post("/api/invitations", status_code=201, response_model=InvitationOut)
    def invite(request: Request, _: CurrentAdministrator, new: NewInvitation) -> object:
        try:
            return InvitationOut.of(accounts.invite(new.email))
        except InvitationRefused as refusal:
            return refused(request, refusal)

    @router.delete("/api/invitations/{email}", status_code=204)
    def revoke(_: CurrentAdministrator, email: str) -> Response:
        if not accounts.revoke_invitation(email):
            raise HTTPException(404)
        return Response(status_code=204)

    @router.get("/users", response_class=HTMLResponse)
    def users_list(request: Request, user: CurrentAdministrator) -> HTMLResponse:
        return users_page(request, user)

    @router.post("/invitations", response_model=None)
    def invite_from_form(
        request: Request, user: CurrentAdministrator, email: Annotated[str, Form()] = ""
    ) -> Response:
        try:
            accounts.invite(email)
        except InvitationRefused as refusal:
            return users_page(
                request,
                user,
                email=email,
                problem=f"invitation_refused.{refusal.reason}.body",
                status_code=_status_code(refusal),
            )
        return RedirectResponse("/users", status_code=303)

    @router.post("/invitations/{email}/revoke", response_model=None)
    def revoke_from_page(_: CurrentAdministrator, email: str) -> Response:
        if not accounts.revoke_invitation(email):
            raise HTTPException(404)
        return RedirectResponse("/users", status_code=303)

    @router.get("/users/{email}", response_class=HTMLResponse)
    def user_details(
        request: Request, user: CurrentAdministrator, email: str, changed: Role | None = None
    ) -> HTMLResponse:
        return user_page(request, user, found(email), changed=changed)

    @router.post("/users/{email}/role", response_model=None)
    def change_role_from_page(
        request: Request, user: CurrentAdministrator, email: str, role: Annotated[Role, Form()]
    ) -> Response:
        shown = found(email)
        try:
            changed = accounts.change_role(email, role)
        except LastAdministrator:
            return last_administrator_page(request, user, shown)
        if changed is None:
            raise HTTPException(404)
        if changed.email == user.email and role == Role.USER:
            return RedirectResponse("/", status_code=303)  # this page is no longer theirs
        return RedirectResponse(f"/users/{quote(changed.email)}?changed={role}", status_code=303)

    @router.post("/users/{email}/remove", response_model=None)
    def remove_from_page(
        request: Request,
        user: CurrentAdministrator,
        email: str,
        confirm: Annotated[bool, Form()] = False,
    ) -> Response:
        shown = found(email)
        if not confirm:  # what it deletes cannot be recovered
            problem = "user.remove.unconfirmed"
            return user_page(request, user, shown, problem=problem, status_code=422)
        try:
            removed = remove(email)
        except LastAdministrator:
            return last_administrator_page(request, user, shown)
        if not removed:
            raise HTTPException(404)
        return RedirectResponse("/" if shown.email == user.email else "/users", status_code=303)

    return router


def _status_code(refusal: InvitationRefused) -> int:
    return 409 if refusal.reason in _CONFLICTS else 422
