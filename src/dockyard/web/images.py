"""Images: Administrators define, follow, rebuild and delete them; every User lists those ready
to pick.

Every route here is for Administrators only, except the list of Images, where a User sees only the
ready ones.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from starlette.responses import Response

from ..accounts import Role, User
from ..images import (
    BaseImageNotBuilt,
    BaseImageUndeletable,
    Image,
    ImageBuilding,
    ImageInUse,
    ImageRefused,
    Images,
    UsingProject,
)
from .guard import CurrentAdministrator, CurrentUser
from .pages import Pages


class NewImage(BaseModel):
    label: str
    description: str = ""
    instructions: str  # only the extra build instructions, run as root on top of the Base Image


class ImageOut(BaseModel):
    id: str
    label: str
    description: str
    base: bool
    status: str

    @classmethod
    def of(cls, image: Image) -> ImageOut:
        return cls(
            id=image.id,
            label=image.label,
            description=image.description,
            base=image.base,
            status=image.status,
        )


class ImageDetailsOut(ImageOut):
    """An Image as its Administrators follow it."""

    instructions: str
    log: str
    failure: str | None

    @classmethod
    def of_details(cls, image: Image) -> ImageDetailsOut:
        return cls(
            instructions=image.instructions,
            log=image.log,
            failure=image.failure,
            **ImageOut.of(image).model_dump(),
        )


def images_routes(images: Images, pages: Pages) -> APIRouter:
    router = APIRouter()

    def refused(request: Request, refusal: ImageRefused) -> Response:
        return pages.problem(
            request, refusal.reason, key=f"image_refused.{refusal.reason}", status_code=422
        )

    def new_image_page(
        request: Request,
        user: User,
        *,
        label: str = "",
        description: str = "",
        instructions: str = "",
        problem: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        return pages.render(
            request,
            "image_new.html",
            status_code=status_code,
            user=user,
            label=label,
            description=description,
            instructions=instructions,
            problem=problem,
        )

    def image_page(
        request: Request,
        user: User,
        image: Image,
        *,
        problem: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        return pages.render(
            request,
            "image.html",
            status_code=status_code,
            user=user,
            image=image,
            used_by=images.used_by(image.id),
            problem=problem,
        )

    def found(image: str) -> Image:
        shown = images.get(image)
        if shown is None:
            raise HTTPException(404)
        return shown

    @router.get("/api/images")
    def list_images(user: CurrentUser) -> list[ImageOut]:
        shown = images.list_all() if user.role == Role.ADMINISTRATOR else images.ready()
        return [ImageOut.of(image) for image in shown]

    @router.post("/api/images", status_code=202, response_model=ImageDetailsOut)
    def create_image(request: Request, _: CurrentAdministrator, new: NewImage) -> object:
        try:
            image = images.create(
                label=new.label, description=new.description, instructions=new.instructions
            )
        except ImageRefused as refusal:
            return refused(request, refusal)
        return ImageDetailsOut.of_details(image)

    @router.get("/api/images/{image}")
    def get_image(_: CurrentAdministrator, image: str) -> ImageDetailsOut:
        return ImageDetailsOut.of_details(found(image))

    @router.post("/api/images/{image}/rebuild", status_code=202, response_model=ImageDetailsOut)
    def rebuild_image(request: Request, _: CurrentAdministrator, image: str) -> object:
        try:
            rebuilt = images.rebuild(image)
        except BaseImageNotBuilt:
            error = "base_image_not_built"
            return pages.problem(request, error, key=error, status_code=409)
        except ImageBuilding:
            error = "building"
            return pages.problem(request, error, key="image_building", status_code=409)
        if rebuilt is None:
            raise HTTPException(404)
        return ImageDetailsOut.of_details(rebuilt)

    @router.delete("/api/images/{image}", status_code=204)
    def delete_image(request: Request, _: CurrentAdministrator, image: str) -> Response:
        try:
            deleted = images.delete(image)
        except BaseImageUndeletable:
            error = "base_image"
            return pages.problem(request, error, key=error, status_code=409)
        except ImageInUse as in_use:
            return pages.problem(
                request,
                "in_use",
                key="image_in_use",
                status_code=409,
                projects=using(in_use.projects),
            )
        if not deleted:
            raise HTTPException(404)
        return Response(status_code=204)

    @router.get("/images", response_class=HTMLResponse)
    def images_page(request: Request, user: CurrentAdministrator) -> HTMLResponse:
        return pages.render(request, "images.html", user=user, images=images.list_all())

    @router.get("/images/new", response_class=HTMLResponse)
    def new_image_form(request: Request, user: CurrentAdministrator) -> HTMLResponse:
        return new_image_page(request, user)

    @router.post("/images", response_model=None)
    def create_from_form(
        request: Request,
        user: CurrentAdministrator,
        label: Annotated[str, Form()] = "",
        description: Annotated[str, Form()] = "",
        instructions: Annotated[str, Form()] = "",
    ) -> Response:
        try:
            image = images.create(label=label, description=description, instructions=instructions)
        except ImageRefused as refusal:
            return new_image_page(
                request,
                user,
                label=label,
                description=description,
                instructions=instructions,
                problem=f"image_refused.{refusal.reason}.body",
                status_code=422,
            )
        return RedirectResponse(f"/images/{image.id}", status_code=303)

    @router.get("/images/{image}", response_class=HTMLResponse)
    def image_details(request: Request, user: CurrentAdministrator, image: str) -> HTMLResponse:
        return image_page(request, user, found(image))

    @router.post("/images/{image}/rebuild", response_model=None)
    def rebuild_from_page(request: Request, user: CurrentAdministrator, image: str) -> Response:
        shown = found(image)
        try:
            rebuilt = images.rebuild(image)
        except BaseImageNotBuilt:
            problem = "base_image_not_built.body"
            return image_page(request, user, shown, problem=problem, status_code=409)
        except ImageBuilding:
            problem = "image_building.body"
            return image_page(request, user, shown, problem=problem, status_code=409)
        if rebuilt is None:
            raise HTTPException(404)
        return RedirectResponse(f"/images/{image}", status_code=303)

    @router.post("/images/{image}/delete", response_model=None)
    def delete_from_page(request: Request, user: CurrentAdministrator, image: str) -> Response:
        shown = found(image)
        try:
            deleted = images.delete(image)
        except BaseImageUndeletable:
            return image_page(request, user, shown, problem="base_image.body", status_code=409)
        except ImageInUse:
            problem = "image.in_use_refused"
            return image_page(request, user, shown, problem=problem, status_code=409)
        if not deleted:
            raise HTTPException(404)
        return RedirectResponse("/images", status_code=303)

    return router


def using(projects: list[UsingProject]) -> str:
    """The Projects using an Image, as a message lists them."""
    return ", ".join(f"{project.name} ({project.owner})" for project in projects)
