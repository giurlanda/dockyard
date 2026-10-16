"""Every Project on the Instance: an Administrator sees them all, with their owner, status and
resource usage, and stops or deletes any of them.

Every route here is for Administrators only. None of them opens a Project's Terminal, console or
logs: those stay with its owner.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from ..projects import OwnedProject, ProjectRefused, Projects
from .guard import CurrentAdministrator
from .pages import Pages
from .projects import ProjectOut, StatsOut


class OwnedProjectOut(ProjectOut):
    owner: str  # its owner's email

    @classmethod
    def of_owned(cls, owned: OwnedProject) -> OwnedProjectOut:
        return cls(owner=owned.owner, **ProjectOut.of(owned.project).model_dump())


def all_projects_routes(projects: Projects, pages: Pages) -> APIRouter:
    router = APIRouter()

    def refused(request: Request, refusal: ProjectRefused) -> Response:
        key = f"project_refused.{refusal.reason}"
        return pages.problem(request, refusal.reason, key=key, status_code=409)

    @router.get("/api/administration/projects")
    def list_all_projects(_: CurrentAdministrator) -> list[OwnedProjectOut]:
        return [OwnedProjectOut.of_owned(owned) for owned in projects.list_all()]

    @router.get("/api/administration/projects/{project}/stats", response_model=StatsOut)
    def get_any_stats(request: Request, _: CurrentAdministrator, project: str) -> object:
        try:
            stats = projects.stats_any(project)
        except ProjectRefused as refusal:  # not running
            return refused(request, refusal)
        if stats is None:
            raise HTTPException(404)
        return StatsOut.of(stats)

    @router.post("/api/administration/projects/{project}/stop")
    def stop_any_project(_: CurrentAdministrator, project: str) -> OwnedProjectOut:
        stopped = projects.stop_any(project)
        if stopped is None:
            raise HTTPException(404)
        return OwnedProjectOut.of_owned(stopped)

    @router.delete("/api/administration/projects/{project}", status_code=204)
    def delete_any_project(
        _: CurrentAdministrator, project: str, delete_code: bool = False
    ) -> None:
        if not projects.delete_any(project, code=delete_code):
            raise HTTPException(404)

    @router.get("/administration/projects", response_class=HTMLResponse)
    def all_projects_page(request: Request, user: CurrentAdministrator) -> HTMLResponse:
        return pages.render(
            request, "all_projects.html", user=user, owned_projects=projects.list_all()
        )

    @router.post("/administration/projects/{project}/stop")
    def stop_from_page(_: CurrentAdministrator, project: str) -> RedirectResponse:
        if projects.stop_any(project) is None:
            raise HTTPException(404)
        return RedirectResponse("/administration/projects", status_code=303)

    @router.post("/administration/projects/{project}/delete")
    def delete_from_page(
        _: CurrentAdministrator, project: str, delete_code: Annotated[bool, Form()] = False
    ) -> RedirectResponse:
        if not projects.delete_any(project, code=delete_code):
            raise HTTPException(404)
        return RedirectResponse("/administration/projects", status_code=303)

    return router
