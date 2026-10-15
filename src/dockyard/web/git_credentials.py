"""Git Credentials: a User adds, renames, rotates and deletes those in their profile.

A User only ever reaches their own Git Credentials: anyone else's answer "not found". No response
ever contains a token.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from starlette.responses import Response

from ..accounts import User
from ..git_credentials import (
    GitCredential,
    GitCredentialInUse,
    GitCredentialRefused,
    GitCredentials,
)
from ..providers import Provider
from .guard import CurrentUser
from .pages import Pages

Change = Literal["label", "token"]


class NewGitCredential(BaseModel):
    label: str
    provider: Provider
    token: str


class GitCredentialChange(BaseModel):
    """Only what is given changes."""

    label: str | None = None
    token: str | None = None


class GitCredentialOut(BaseModel):
    id: str
    label: str
    provider: str
    created_at: str
    updated_at: str

    @classmethod
    def of(cls, credential: GitCredential) -> GitCredentialOut:
        return cls(
            id=credential.id,
            label=credential.label,
            provider=credential.provider,
            created_at=credential.created_at,
            updated_at=credential.updated_at,
        )


def git_credentials_routes(credentials: GitCredentials, pages: Pages) -> APIRouter:
    router = APIRouter()

    def refused(request: Request, refusal: GitCredentialRefused) -> Response:
        return pages.problem(
            request,
            refusal.reason,
            key=f"git_credential_refused.{refusal.reason}",
            status_code=422,
        )

    def new_credential_page(
        request: Request,
        user: User,
        *,
        label: str = "",
        provider: Provider = Provider.GITHUB,
        problem: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        # Never the token: not even one that was refused.
        return pages.render(
            request,
            "git_credential_new.html",
            status_code=status_code,
            user=user,
            label=label,
            providers=list(Provider),
            chosen=provider,
            problem=problem,
        )

    def show_credential(
        request: Request,
        user: User,
        credential: GitCredential,
        *,
        changed: Change | None = None,
        problem: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        return pages.render(
            request,
            "git_credential.html",
            status_code=status_code,
            user=user,
            credential=credential,
            used_by=credentials.used_by(user, credential.id),
            changed=changed,
            problem=problem,
        )

    def change_from_page(
        request: Request, user: User, credential: str, change: Change, value: str
    ) -> Response:
        """Rename the Git Credential or rotate its token, then show its page."""
        found = credentials.get(user, credential)
        if found is None:
            raise HTTPException(404)
        try:
            changed = credentials.update(user, credential, **{change: value})
        except GitCredentialRefused as refusal:
            return show_credential(request, user, found, problem=_problem(refusal), status_code=422)
        if changed is None:
            raise HTTPException(404)
        return RedirectResponse(f"/git-credentials/{credential}?changed={change}", status_code=303)

    @router.get("/api/git-credentials")
    def list_credentials(user: CurrentUser) -> list[GitCredentialOut]:
        return [GitCredentialOut.of(credential) for credential in credentials.list(user)]

    @router.post("/api/git-credentials", status_code=201, response_model=GitCredentialOut)
    def add_credential(request: Request, user: CurrentUser, new: NewGitCredential) -> object:
        try:
            credential = credentials.create(
                user, label=new.label, provider=new.provider, token=new.token
            )
        except GitCredentialRefused as refusal:
            return refused(request, refusal)
        return GitCredentialOut.of(credential)

    @router.get("/api/git-credentials/{credential}")
    def get_credential(user: CurrentUser, credential: str) -> GitCredentialOut:
        found = credentials.get(user, credential)
        if found is None:
            raise HTTPException(404)
        return GitCredentialOut.of(found)

    @router.patch("/api/git-credentials/{credential}", response_model=GitCredentialOut)
    def change_credential(
        request: Request, user: CurrentUser, credential: str, change: GitCredentialChange
    ) -> object:
        try:
            changed = credentials.update(user, credential, label=change.label, token=change.token)
        except GitCredentialRefused as refusal:
            return refused(request, refusal)
        if changed is None:
            raise HTTPException(404)
        return GitCredentialOut.of(changed)

    @router.delete("/api/git-credentials/{credential}", status_code=204)
    def delete_credential(request: Request, user: CurrentUser, credential: str) -> Response:
        try:
            deleted = credentials.delete(user, credential)
        except GitCredentialInUse as in_use:
            return pages.problem(
                request,
                "in_use",
                key="git_credential_in_use",
                status_code=409,
                projects=", ".join(in_use.projects),
            )
        if not deleted:
            raise HTTPException(404)
        return Response(status_code=204)

    @router.get("/git-credentials/new", response_class=HTMLResponse)
    def new_credential_form(request: Request, user: CurrentUser) -> HTMLResponse:
        return new_credential_page(request, user)

    @router.post("/git-credentials", response_model=None)
    def add_from_form(
        request: Request,
        user: CurrentUser,
        label: Annotated[str, Form()] = "",
        provider: Annotated[Provider, Form()] = Provider.GITHUB,
        token: Annotated[str, Form()] = "",
    ) -> Response:
        try:
            credentials.create(user, label=label, provider=provider, token=token)
        except GitCredentialRefused as refusal:
            return new_credential_page(
                request,
                user,
                label=label,
                provider=provider,
                problem=_problem(refusal),
                status_code=422,
            )
        return RedirectResponse("/", status_code=303)

    @router.get("/git-credentials/{credential}", response_class=HTMLResponse)
    def credential_page(
        request: Request, user: CurrentUser, credential: str, changed: Change | None = None
    ) -> HTMLResponse:
        found = credentials.get(user, credential)
        if found is None:
            raise HTTPException(404)
        return show_credential(request, user, found, changed=changed)

    @router.post("/git-credentials/{credential}/label", response_model=None)
    def rename_from_page(
        request: Request, user: CurrentUser, credential: str, label: Annotated[str, Form()] = ""
    ) -> Response:
        return change_from_page(request, user, credential, "label", label)

    @router.post("/git-credentials/{credential}/token", response_model=None)
    def rotate_from_page(
        request: Request, user: CurrentUser, credential: str, token: Annotated[str, Form()] = ""
    ) -> Response:
        return change_from_page(request, user, credential, "token", token)

    @router.post("/git-credentials/{credential}/delete", response_model=None)
    def delete_from_page(request: Request, user: CurrentUser, credential: str) -> Response:
        try:
            deleted = credentials.delete(user, credential)
        except GitCredentialInUse:
            found = credentials.get(user, credential)
            if found is None:
                raise HTTPException(404) from None
            return show_credential(
                request, user, found, problem="git_credential.in_use_refused", status_code=409
            )
        if not deleted:
            raise HTTPException(404)
        return RedirectResponse("/", status_code=303)

    return router


def _problem(refusal: GitCredentialRefused) -> str:
    """Why the form was refused, as the key of its message."""
    return f"git_credential_refused.{refusal.reason}.body"
