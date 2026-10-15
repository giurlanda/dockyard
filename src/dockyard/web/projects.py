"""Projects: create one from a repository URL, follow its clone, run, list and delete them.

A User only ever reaches their own Projects: anyone else's answer "not found".
"""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from starlette.responses import Response

from ..accounts import User
from ..projects import (
    CloneReport,
    PermissionMode,
    Project,
    ProjectRefused,
    Projects,
    Refusal,
    Visibility,
)
from ..runtime import CloneState
from .guard import CurrentUser
from .pages import Pages

Lifecycle = Literal["start", "stop", "restart"]  # restarting is starting a running Project


class NewProject(BaseModel):
    name: str
    repository: str
    visibility: Visibility
    permission_mode: PermissionMode = PermissionMode.DEFAULT


class ProjectOut(BaseModel):
    id: str
    name: str
    repository: str
    provider: str
    visibility: str
    permission_mode: str
    status: str

    @classmethod
    def of(cls, project: Project) -> ProjectOut:
        return cls(
            id=project.id,
            name=project.name,
            repository=project.repository,
            provider=project.provider,
            visibility=project.visibility,
            permission_mode=project.permission_mode,
            status=project.status,
        )


class CloneOut(BaseModel):
    state: CloneState
    output: str


def projects_routes(projects: Projects, pages: Pages) -> APIRouter:
    router = APIRouter()

    def refused(request: Request, refusal: ProjectRefused) -> Response:
        status_code = 409 if refusal.reason == Refusal.CLONING else 422
        return pages.problem(
            request,
            refusal.reason,
            key=f"project_refused.{refusal.reason}",
            status_code=status_code,
        )

    def new_project_page(
        request: Request,
        user: User,
        *,
        name: str,
        repository: str,
        permission_mode: PermissionMode = PermissionMode.DEFAULT,
        problem: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        return pages.render(
            request,
            "project_new.html",
            status_code=status_code,
            user=user,
            name=name,
            repository=repository,
            permission_modes=list(PermissionMode),
            permission_mode=permission_mode,
            problem=problem,
        )

    def run(user: User, project: str, action: Lifecycle) -> Project:
        """Start, stop or restart the Project. Raises ProjectRefused, HTTPException."""
        found = projects.stop(user, project) if action == "stop" else projects.start(user, project)
        if found is None:
            raise HTTPException(404)
        return found

    @router.get("/api/projects")
    def list_projects(user: CurrentUser) -> list[ProjectOut]:
        return [ProjectOut.of(project) for project in projects.list(user)]

    @router.post("/api/projects", status_code=202, response_model=ProjectOut)
    def create_project(request: Request, user: CurrentUser, new: NewProject) -> object:
        try:
            project = projects.create(
                user,
                name=new.name,
                repository=new.repository,
                visibility=new.visibility,
                permission_mode=new.permission_mode,
            )
        except ProjectRefused as refusal:
            return refused(request, refusal)
        return ProjectOut.of(project)

    @router.get("/api/projects/{project}")
    def get_project(user: CurrentUser, project: str) -> ProjectOut:
        found = projects.get(user, project)
        if found is None:
            raise HTTPException(404)
        return ProjectOut.of(found)

    @router.get("/api/projects/{project}/clone")
    def get_clone(user: CurrentUser, project: str) -> CloneOut:
        report = projects.clone(user, project)
        if report is None:
            raise HTTPException(404)
        return CloneOut(state=report.state, output=report.output)

    @router.post("/api/projects/{project}/{action}", response_model=ProjectOut)
    def run_project(request: Request, user: CurrentUser, project: str, action: Lifecycle) -> object:
        try:
            return ProjectOut.of(run(user, project, action))
        except ProjectRefused as refusal:
            return refused(request, refusal)

    @router.delete("/api/projects/{project}", status_code=204)
    def delete_project(user: CurrentUser, project: str, delete_code: bool = False) -> None:
        if not projects.delete(user, project, code=delete_code):
            raise HTTPException(404)

    @router.get("/projects/new", response_class=HTMLResponse)
    def new_project_form(
        request: Request, user: CurrentUser, name: str = "", repository: str = ""
    ) -> HTMLResponse:
        return new_project_page(request, user, name=name, repository=repository)

    @router.post("/projects", response_model=None)
    def create_from_form(
        request: Request,
        user: CurrentUser,
        name: Annotated[str, Form()] = "",
        repository: Annotated[str, Form()] = "",
        visibility: Annotated[Visibility, Form()] = Visibility.PUBLIC,
        permission_mode: Annotated[PermissionMode, Form()] = PermissionMode.DEFAULT,
    ) -> Response:
        try:
            project = projects.create(
                user,
                name=name,
                repository=repository,
                visibility=visibility,
                permission_mode=permission_mode,
            )
        except ProjectRefused as refusal:
            return new_project_page(
                request,
                user,
                name=name,
                repository=repository,
                permission_mode=permission_mode,
                problem=f"project_refused.{refusal.reason}.body",
                status_code=422,
            )
        return RedirectResponse(f"/projects/{project.id}", status_code=303)

    @router.get("/projects/{project}", response_class=HTMLResponse)
    def project_page(request: Request, user: CurrentUser, project: str) -> HTMLResponse:
        found = projects.get(user, project)
        report = projects.clone(user, project)
        if found is not None:
            return pages.render(request, "project.html", user=user, project=found, clone=report)
        if report is not None and report.state == CloneState.FAILED:
            return pages.render(
                request, "clone_failed.html", user=user, report=report, retry=_retry(report)
            )
        raise HTTPException(404)

    @router.post("/projects/{project}/delete")
    def delete_from_page(
        user: CurrentUser, project: str, delete_code: Annotated[bool, Form()] = False
    ) -> RedirectResponse:
        if not projects.delete(user, project, code=delete_code):
            raise HTTPException(404)
        return RedirectResponse("/", status_code=303)

    @router.post("/projects/{project}/{action}", response_model=None)
    def run_from_page(
        request: Request, user: CurrentUser, project: str, action: Lifecycle
    ) -> Response:
        try:
            run(user, project, action)
        except ProjectRefused as refusal:
            return refused(request, refusal)
        return RedirectResponse(f"/projects/{project}", status_code=303)

    return router


def _retry(report: CloneReport) -> str:
    """The new Project form, filled in as it was for the clone that failed."""
    return "/projects/new?" + urlencode({"name": report.name, "repository": report.repository})
