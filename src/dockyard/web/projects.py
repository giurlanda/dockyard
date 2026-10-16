"""Projects: create one from a repository URL, follow its clone, run, list and delete them.

A User only ever reaches their own Projects: anyone else's answer "not found".
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from http import HTTPStatus
from typing import Annotated, Literal
from urllib.parse import urlencode

import anyio
from fastapi import APIRouter, Form, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from starlette.responses import Response

from ..accounts import User
from ..git_credentials import GitCredentials
from ..projects import (
    GIB,
    MIN_CPUS,
    MIN_MEMORY,
    CloneReport,
    Limits,
    PermissionMode,
    Project,
    ProjectRefused,
    Projects,
    Refusal,
    Visibility,
)
from ..providers import Provider
from ..runtime import CloneState, Output, RuntimeUnavailable, Stats
from .guard import CurrentUser, websocket_close_code
from .pages import Pages
from .terminal import bridge, show

log = logging.getLogger(__name__)

Lifecycle = Literal["start", "stop", "restart"]  # restarting is starting a running Project


# The form shows RAM in GiB; the API takes and gives bytes.
class NewProject(BaseModel):
    name: str
    repository: str
    visibility: Visibility
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    git_credential: str | None = None  # the id of one of the User's Git Credentials
    cpus: float | None = None  # None: the most the Instance allows
    memory: int | None = None  # bytes. None: the most the Instance allows


class ProjectOut(BaseModel):
    id: str
    name: str
    repository: str
    provider: str
    visibility: str
    permission_mode: str
    status: str
    git_credential: str | None
    cpus: float
    memory: int  # bytes

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
            git_credential=project.git_credential,
            cpus=project.cpus,
            memory=project.memory,
        )


class CloneOut(BaseModel):
    state: CloneState
    output: str


class StatsOut(BaseModel):
    cpu: float  # percent of one CPU: 2 busy CPUs make 200
    cpus: float  # the CPUs it may use
    memory: int  # bytes
    memory_limit: int  # bytes

    @classmethod
    def of(cls, stats: Stats) -> StatsOut:
        return cls(
            cpu=stats.cpu, cpus=stats.cpus, memory=stats.memory, memory_limit=stats.memory_limit
        )


def projects_routes(projects: Projects, git_credentials: GitCredentials, pages: Pages) -> APIRouter:
    router = APIRouter()

    conflicts = (Refusal.CLONING, Refusal.NOT_RUNNING, Refusal.ACTIVE_MAXIMUM)
    limits = limits_shown(projects.limits)

    def refused(request: Request, refusal: ProjectRefused) -> Response:
        status_code = 409 if refusal.reason in conflicts else 422
        return pages.problem(
            request,
            refusal.reason,
            key=f"project_refused.{refusal.reason}",
            status_code=status_code,
            **limits,
        )

    def new_project_page(
        request: Request,
        user: User,
        *,
        name: str,
        repository: str,
        visibility: Visibility = Visibility.PUBLIC,
        git_credential: str = "",
        permission_mode: PermissionMode = PermissionMode.DEFAULT,
        cpus: str = "",  # "": the most the Instance allows
        memory: str = "",  # in GiB. "": the most the Instance allows
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
            visibilities=list(Visibility),
            visibility=visibility,
            providers=list(Provider),
            git_credentials=git_credentials.list(user),
            git_credential=git_credential,
            permission_modes=list(PermissionMode),
            permission_mode=permission_mode,
            cpus=cpus or limits["max_cpus"],
            memory=memory or limits["max_memory"],
            limits=limits,
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
                git_credential=new.git_credential,
                cpus=new.cpus,
                memory=new.memory,
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

    @router.get("/api/projects/{project}/stats", response_model=StatsOut)
    def get_stats(request: Request, user: CurrentUser, project: str) -> object:
        try:
            stats = projects.stats(user, project)
        except ProjectRefused as refusal:
            return refused(request, refusal)
        if stats is None:
            raise HTTPException(404)
        return StatsOut.of(stats)

    @router.websocket("/ws/projects/{project}/logs")
    async def logs(websocket: WebSocket, user: CurrentUser, project: str) -> None:
        if output := await opened(websocket, lambda: projects.follow_log(user, project)):
            await show(websocket, output)

    @router.websocket("/ws/projects/{project}/console")
    async def console(websocket: WebSocket, user: CurrentUser, project: str) -> None:
        if tty := await opened(websocket, lambda: projects.attach_console(user, project)):
            await bridge(websocket, tty)

    @router.websocket("/ws/projects/{project}/terminal")
    async def terminal(websocket: WebSocket, user: CurrentUser, project: str) -> None:
        if tty := await opened(websocket, lambda: projects.open_terminal(user, project)):
            await bridge(websocket, tty)

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
        request: Request,
        user: CurrentUser,
        name: str = "",
        repository: str = "",
        visibility: Visibility = Visibility.PUBLIC,
        git_credential: str = "",
        cpus: str = "",
        memory: str = "",
    ) -> HTMLResponse:
        return new_project_page(
            request,
            user,
            name=name,
            repository=repository,
            visibility=visibility,
            git_credential=git_credential,
            cpus=cpus,
            memory=memory,
        )

    @router.post("/projects", response_model=None)
    def create_from_form(
        request: Request,
        user: CurrentUser,
        name: Annotated[str, Form()] = "",
        repository: Annotated[str, Form()] = "",
        visibility: Annotated[Visibility, Form()] = Visibility.PUBLIC,
        git_credential: Annotated[str, Form()] = "",  # "": none
        permission_mode: Annotated[PermissionMode, Form()] = PermissionMode.DEFAULT,
        cpus: Annotated[str, Form()] = "",  # "": the most the Instance allows
        memory: Annotated[str, Form()] = "",  # in GiB. "": the most the Instance allows
    ) -> Response:
        try:
            chosen_memory = _amount(memory, Refusal.MEMORY_OUT_OF_RANGE)
            project = projects.create(
                user,
                name=name,
                repository=repository,
                visibility=visibility,
                permission_mode=permission_mode,
                git_credential=git_credential or None,
                cpus=_amount(cpus, Refusal.CPUS_OUT_OF_RANGE),
                memory=None if chosen_memory is None else round(chosen_memory * GIB),
            )
        except ProjectRefused as refusal:
            return new_project_page(
                request,
                user,
                name=name,
                repository=repository,
                visibility=visibility,
                git_credential=git_credential,
                permission_mode=permission_mode,
                cpus=cpus,
                memory=memory,
                problem=f"project_refused.{refusal.reason}.body",
                status_code=422,
            )
        return RedirectResponse(f"/projects/{project.id}", status_code=303)

    @router.get("/projects/{project}", response_class=HTMLResponse)
    def project_page(request: Request, user: CurrentUser, project: str) -> HTMLResponse:
        found = projects.get(user, project)
        report = projects.clone(user, project)
        if found is not None:
            credential = (
                git_credentials.get(user, found.git_credential) if found.git_credential else None
            )
            return pages.render(
                request,
                "project.html",
                user=user,
                project=found,
                clone=report,
                git_credential=credential,
            )
        if report is not None and report.state == CloneState.FAILED:
            return pages.render(
                request, "clone_failed.html", user=user, report=report, retry=_retry(report)
            )
        raise HTTPException(404)

    @router.get("/projects/{project}/logs", response_class=HTMLResponse)
    def logs_page(request: Request, user: CurrentUser, project: str) -> HTMLResponse:
        return following_page(request, user, project, "project_logs.html")

    @router.get("/projects/{project}/console", response_class=HTMLResponse)
    def console_page(request: Request, user: CurrentUser, project: str) -> HTMLResponse:
        return following_page(request, user, project, "project_console.html")

    @router.get("/projects/{project}/terminal", response_class=HTMLResponse)
    def terminal_page(request: Request, user: CurrentUser, project: str) -> HTMLResponse:
        return following_page(request, user, project, "project_terminal.html")

    def following_page(request: Request, user: User, project: str, template: str) -> HTMLResponse:
        found = projects.get(user, project)
        if found is None:
            raise HTTPException(404)
        return pages.render(request, template, user=user, project=found)

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


async def opened[T: Output](websocket: WebSocket, opening: Callable[[], T | None]) -> T | None:
    """Accept the socket and open what it shows; if that cannot be, close it saying why."""
    await websocket.accept()
    try:
        output = await anyio.to_thread.run_sync(opening)
    except ProjectRefused:  # not running
        await websocket.close(websocket_close_code(HTTPStatus.CONFLICT))
        return None
    except RuntimeUnavailable as error:
        log.warning("The Runtime is unavailable: %s", error)
        await websocket.close(websocket_close_code(HTTPStatus.SERVICE_UNAVAILABLE))
        return None
    if output is None:
        await websocket.close(websocket_close_code(HTTPStatus.NOT_FOUND))
    return output


def limits_shown(limits: Limits) -> dict[str, str]:
    """The Instance limits as messages and the new Project form show them: RAM in GiB."""
    return {
        "min_cpus": _number(MIN_CPUS),
        "max_cpus": _number(limits.max_cpus),
        "min_memory": _number(MIN_MEMORY / GIB),
        "max_memory": _number(limits.max_memory / GIB),
        "max_active": str(limits.max_active),
    }


def _number(value: float) -> str:
    return f"{value:g}"


def _amount(text: str, refusal: Refusal) -> float | None:
    """An amount typed in the form; None when left empty. Raises ProjectRefused(refusal)."""
    if not text.strip():
        return None
    try:
        amount = float(text)
    except ValueError:
        raise ProjectRefused(refusal) from None
    if not math.isfinite(amount):
        raise ProjectRefused(refusal)
    return amount


def _retry(report: CloneReport) -> str:
    """The new Project form, filled in as it was for the clone that failed."""
    filled = {
        "name": report.name,
        "repository": report.repository,
        "visibility": report.visibility,
        "git_credential": report.git_credential or "",
        "cpus": "" if report.cpus is None else _number(report.cpus),
        "memory": "" if report.memory is None else _number(report.memory / GIB),
    }
    return "/projects/new?" + urlencode(filled)
