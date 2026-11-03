"""Skills: Administrators upload and delete them; every User lists them and reads each one.

Uploading and deleting, and the page listing the catalog, are for Administrators only.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from starlette.responses import Response

from ..accounts import User
from ..skills import UPLOAD_MAX_BYTES, Skill, SkillNameTaken, SkillRefused, Skills
from .guard import CurrentAdministrator, CurrentUser
from .pages import Pages


class SkillOut(BaseModel):
    id: str
    name: str
    description: str
    updated_at: str

    @classmethod
    def of(cls, skill: Skill) -> SkillOut:
        return cls(
            id=skill.id,
            name=skill.name,
            description=skill.description,
            updated_at=skill.updated_at,
        )


class SkillFileOut(BaseModel):
    path: str
    size: int
    content: str | None  # None when it is not text


class SkillDetailsOut(SkillOut):
    files: list[SkillFileOut]

    @classmethod
    def of_details(cls, skill: Skill) -> SkillDetailsOut:
        files = [SkillFileOut(path=f.path, size=f.size, content=f.content) for f in skill.files]
        return cls(files=files, **SkillOut.of(skill).model_dump())


def skills_routes(skills: Skills, pages: Pages) -> APIRouter:
    router = APIRouter()

    def create(file: UploadFile) -> Skill:
        """Raises SkillRefused, SkillNameTaken."""
        # One byte more than allowed is enough to refuse it: never read all of a huge upload.
        return skills.create(file.file.read(UPLOAD_MAX_BYTES + 1), file.filename or "")

    def refused(request: Request, refusal: SkillRefused) -> Response:
        return pages.problem(
            request, refusal.reason, key=f"skill_refused.{refusal.reason}", status_code=422
        )

    def skills_page(
        request: Request, user: User, *, problem: str | None = None, status_code: int = 200
    ) -> HTMLResponse:
        return pages.render(
            request,
            "skills.html",
            status_code=status_code,
            user=user,
            skills=skills.list_all(),
            problem=problem,
        )

    def found(skill: str) -> Skill:
        shown = skills.get(skill)
        if shown is None:
            raise HTTPException(404)
        return shown

    @router.get("/api/skills")
    def list_skills(_: CurrentUser) -> list[SkillOut]:
        return [SkillOut.of(skill) for skill in skills.list_all()]

    @router.post("/api/skills", status_code=201, response_model=SkillDetailsOut)
    def upload_skill(
        request: Request, _: CurrentAdministrator, file: Annotated[UploadFile, File()]
    ) -> object:
        try:
            skill = create(file)
        except SkillRefused as refusal:
            return refused(request, refusal)
        except SkillNameTaken:
            error = "name_taken"
            return pages.problem(request, error, key="skill_name_taken", status_code=409)
        return SkillDetailsOut.of_details(skill)

    @router.get("/api/skills/{skill}")
    def get_skill(_: CurrentUser, skill: str) -> SkillDetailsOut:
        return SkillDetailsOut.of_details(found(skill))

    @router.delete("/api/skills/{skill}", status_code=204)
    def delete_skill(_: CurrentAdministrator, skill: str) -> Response:
        if not skills.delete(skill):
            raise HTTPException(404)
        return Response(status_code=204)

    @router.get("/skills", response_class=HTMLResponse)
    def catalog_page(request: Request, user: CurrentAdministrator) -> HTMLResponse:
        return skills_page(request, user)

    @router.post("/skills", response_model=None)
    def upload_from_page(
        request: Request, user: CurrentAdministrator, file: Annotated[UploadFile, File()]
    ) -> Response:
        try:
            skill = create(file)
        except SkillRefused as refusal:
            problem = f"skill_refused.{refusal.reason}.body"
            return skills_page(request, user, problem=problem, status_code=422)
        except SkillNameTaken:
            return skills_page(request, user, problem="skill_name_taken.body", status_code=409)
        return RedirectResponse(f"/skills/{skill.id}", status_code=303)

    @router.get("/skills/{skill}", response_class=HTMLResponse)
    def skill_page(request: Request, user: CurrentUser, skill: str) -> HTMLResponse:
        return pages.render(request, "skill.html", user=user, skill=found(skill))

    @router.post("/skills/{skill}/delete", response_model=None)
    def delete_from_page(_: CurrentAdministrator, skill: str) -> Response:
        if not skills.delete(skill):
            raise HTTPException(404)
        return RedirectResponse("/skills", status_code=303)

    return router
