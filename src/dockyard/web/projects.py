"""Projects: create one from a repository URL, follow its clone, list and delete them.

A User only ever reaches their own Projects: anyone else's answer "not found".
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from starlette.responses import Response

from ..projects import CloneReport, Project, ProjectRefused, Projects, Visibility
from ..runtime import CloneState
from .guard import CurrentUser
from .pages import Pages


class NewProject(BaseModel):
    name: str
    repository: str
    visibility: Visibility


class ProjectOut(BaseModel):
    id: str
    name: str
    repository: str
    provider: str
    visibility: str
    status: str

    @classmethod
    def of(cls, project: Project) -> ProjectOut:
        return cls(
            id=project.id,
            name=project.name,
            repository=project.repository,
            provider=project.provider,
            visibility=project.visibility,
            status=project.status,
        )


class CloneOut(BaseModel):
    state: CloneState
    output: str


def projects_routes(projects: Projects, pages: Pages) -> APIRouter:
    router = APIRouter()

    def refused(request: Request, refusal: ProjectRefused) -> Response:
        return pages.problem(
            request, refusal.reason, key=f"project_refused.{refusal.reason}", status_code=422
        )

    @router.get("/api/projects")
    def list_projects(user: CurrentUser) -> list[ProjectOut]:
        return [ProjectOut.of(project) for project in projects.list(user)]

    @router.post("/api/projects", status_code=202, response_model=ProjectOut)
    def create_project(request: Request, user: CurrentUser, new: NewProject) -> object:
        try:
            project = projects.create(
                user, name=new.name, repository=new.repository, visibility=new.visibility
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

    @router.delete("/api/projects/{project}", status_code=204)
    def delete_project(user: CurrentUser, project: str, delete_code: bool = False) -> None:
        if not projects.delete(user, project, code=delete_code):
            raise HTTPException(404)

    @router.get("/projects/new", response_class=HTMLResponse)
    def new_project_form(
        request: Request, user: CurrentUser, name: str = "", repository: str = ""
    ) -> HTMLResponse:
        return pages.render(
            request, "project_new.html", user=user, name=name, repository=repository, problem=None
        )

    @router.post("/projects", response_model=None)
    def create_from_form(
        request: Request,
        user: CurrentUser,
        name: Annotated[str, Form()] = "",
        repository: Annotated[str, Form()] = "",
        visibility: Annotated[Visibility, Form()] = Visibility.PUBLIC,
    ) -> Response:
        try:
            project = projects.create(user, name=name, repository=repository, visibility=visibility)
        except ProjectRefused as refusal:
            return pages.render(
                request,
                "project_new.html",
                status_code=422,
                user=user,
                name=name,
                repository=repository,
                problem=f"project_refused.{refusal.reason}.body",
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

    return router


def _retry(report: CloneReport) -> str:
    """The new Project form, filled in as it was for the clone that failed."""
    return "/projects/new?" + urlencode({"name": report.name, "repository": report.repository})
