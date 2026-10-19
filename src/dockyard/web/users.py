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
    normalise_email,
)
from ..projects import Projects
from ..runtime import Runtime
from .guard import CurrentAdministrator
from .local_sign_in import ActivationLinks
from .pages import Pages

# Refusals of what conflicts with how things are, rather than with what was asked.
_CONFLICTS = {Refusal.ALREADY_USER, Refusal.ALREADY_INVITED}


class NewInvitation(BaseModel):
    email: str


class InvitationOut(BaseModel):
    email: str
    created_at: str
    # Shown once, as it is made: only while Local sign-in is on, and only when just invited.
    activation_link: str | None = None

    @classmethod
    def of(cls, invitation: Invitation, activation_link: str | None = None) -> InvitationOut:
        return cls(
            email=invitation.email,
            created_at=invitation.created_at,
            activation_link=activation_link,
        )


class ActivationLinkOut(BaseModel):
    activation_link: str


class UserChange(BaseModel):
    """What an Administrator changes about a User: what is not given stays as it is."""

    role: Role | None = None
    privileged_projects: bool | None = None


class UserOut(BaseModel):
    email: str
    role: Role
    privileged_projects: bool

    @classmethod
    def of(cls, user: User) -> UserOut:
        return cls(email=user.email, role=user.role, privileged_projects=user.privileged_projects)


def users_routes(
    accounts: Accounts,
    projects: Projects,
    runtime: Runtime,
    pages: Pages,
    activation_links: ActivationLinks | None,  # None: Local sign-in is off
) -> APIRouter:
    router = APIRouter()

    def activation_link(email: str) -> str | None:
        return activation_links.new(email) if activation_links else None

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

    def permit(email: str, permitted: bool) -> User | None:
        """Grant or revoke the User's permission to create Privileged Projects; revoking stops
        their Privileged Projects. None if there is no such User."""
        changed = accounts.permit_privileged_projects(email, permitted)
        if changed is not None and not permitted:
            projects.stop_privileged(changed)
        return changed

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
        made: tuple[str, str] | None = None,  # the email and the Activation link just made
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
            activation_links=activation_links is not None,
            made=made,
        )

    def made_page(request: Request, user: User, email: str, link: str) -> HTMLResponse:
        """The Users page showing the Activation link just made, the only time it is shown."""
        return users_page(request, user, made=(email, link), status_code=201)

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
    def change_user(
        request: Request, _: CurrentAdministrator, email: str, change: UserChange
    ) -> object:
        changed: User | None = found(email)
        if change.role is not None:
            try:
                changed = accounts.change_role(email, change.role)
            except LastAdministrator:
                return last_administrator(request)
        if changed is not None and change.privileged_projects is not None:
            changed = permit(email, change.privileged_projects)
        if changed is None:  # removed meanwhile
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
            invitation = accounts.invite(new.email)
        except InvitationRefused as refusal:
            return refused(request, refusal)
        return InvitationOut.of(invitation, activation_link(invitation.email))

    @router.post("/api/invitations/{email}/activation-link", status_code=201, response_model=None)
    def new_activation_link(_: CurrentAdministrator, email: str) -> ActivationLinkOut:
        link = activation_link(email)
        if link is None:
            raise HTTPException(404)
        return ActivationLinkOut(activation_link=link)

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
            invitation = accounts.invite(email)
        except InvitationRefused as refusal:
            return users_page(
                request,
                user,
                email=email,
                problem=f"invitation_refused.{refusal.reason}.body",
                status_code=_status_code(refusal),
            )
        link = activation_link(invitation.email)
        if link is None:  # Local sign-in is off, or the Invitation is already used up
            return RedirectResponse("/users", status_code=303)
        return made_page(request, user, invitation.email, link)

    @router.post("/invitations/{email}/activation-link", response_model=None)
    def new_activation_link_from_page(
        request: Request, user: CurrentAdministrator, email: str
    ) -> Response:
        link = activation_link(email)
        if link is None:
            raise HTTPException(404)
        return made_page(request, user, normalise_email(email), link)

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

    @router.post("/users/{email}/privileged-projects")
    def permit_from_page(
        _: CurrentAdministrator, email: str, permitted: Annotated[bool, Form()]
    ) -> RedirectResponse:
        changed = permit(found(email).email, permitted)
        if changed is None:  # removed meanwhile
            raise HTTPException(404)
        return RedirectResponse(f"/users/{quote(changed.email)}", status_code=303)

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
