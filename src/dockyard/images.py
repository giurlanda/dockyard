"""Images: the Base Image, and the Images Administrators define on top of it.

An Administrator writes only an Image's extra build instructions: the Runtime builds them on top of
the Base Image, as root, then back to its non-root user. A built Image is checked (Claude Code is
there, its default user is not root) before Users may pick it for a Project.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from dataclasses import dataclass, replace
from enum import StrEnum

from .database import Database
from .runtime import ImageCheck, ImageMissing, Runtime, RuntimeUnavailable

log = logging.getLogger(__name__)

BASE_IMAGE = "base"  # the id of the Base Image, which no Administrator defines
LABEL_MAX_LENGTH = 60
DESCRIPTION_MAX_LENGTH = 500
INSTRUCTIONS_MAX_LENGTH = 20_000
BUILD_LOG_LINES = 500  # the end of what a build printed: why it failed is there
# An instruction that starts another stage would leave the Base Image, and Claude Code, behind.
# Upper case only: a line of a heredoc may start with "from" (Python's, for one).
STARTS_A_STAGE = re.compile(r"^\s*FROM\s", re.MULTILINE)


class ImageStatus(StrEnum):
    BUILDING = "building"  # being built, then checked
    READY = "ready"  # Users may pick it
    FAILED = "failed"


class Failure(StrEnum):
    """Why an Image is not ready."""

    BUILD = "build"  # its build failed: its log says why
    CLAUDE_MISSING = "claude_missing"
    ROOT_USER = "root_user"  # its default user is root, or cannot be told


class Refusal(StrEnum):
    LABEL_MISSING = "label_missing"
    LABEL_INVALID = "label_invalid"
    DESCRIPTION_INVALID = "description_invalid"
    INSTRUCTIONS_MISSING = "instructions_missing"
    INSTRUCTIONS_INVALID = "instructions_invalid"
    INSTRUCTIONS_FROM = "instructions_from"


class ImageRefused(Exception):
    def __init__(self, reason: Refusal) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class UsingProject:
    name: str
    owner: str  # its owner's email


class ImageInUse(Exception):
    """Projects use the Image: it cannot be deleted until they are."""

    def __init__(self, projects: list[UsingProject]) -> None:
        self.projects = projects
        super().__init__(projects)


class BaseImageUndeletable(Exception):
    """The Base Image is shipped with Dockyard: it is never deleted."""


@dataclass(frozen=True)
class Image:
    id: str
    label: str
    description: str
    instructions: str  # the extra build instructions, "" for the Base Image
    status: ImageStatus
    log: str = ""  # what its build printed, then what its check found
    failure: Failure | None = None

    @property
    def base(self) -> bool:
        return self.id == BASE_IMAGE


BASE = Image(
    BASE_IMAGE,
    label="Base Image",
    description="Claude Code, git, gh, glab, Node and Python.",
    instructions="",
    status=ImageStatus.READY,
)


class Images:
    def __init__(self, database: Database, runtime: Runtime) -> None:
        self._db = database
        self._runtime = runtime
        self._lock = threading.Lock()
        self._logs: dict[str, str] = {}  # of the builds in progress

    def create(self, *, label: str, description: str, instructions: str) -> Image:
        """Record the Image and start building it: it is ready once built and checked.

        Raises ImageRefused, RuntimeUnavailable.
        """
        label = valid_label(label)
        description = valid_description(description)
        instructions = valid_instructions(instructions)
        self._runtime.check_reachable()  # a build Docker cannot start would fail for good
        with self._db.transaction() as conn:
            row = conn.execute(
                "INSERT INTO images (label, description, instructions) VALUES (?, ?, ?)"
                f" RETURNING {_COLUMNS}",
                (label, description, instructions),
            ).fetchone()
        self._build_in_background(row["id"], instructions)
        return _image(row)

    def list_all(self) -> list[Image]:
        """Every Image, the Base Image first: for Administrators."""
        with self._db.transaction() as conn:
            rows = conn.execute(f"SELECT {_COLUMNS} FROM images ORDER BY created_at, rowid")
            images = [self._shown(row) for row in rows]
        return [BASE, *images]

    def ready(self) -> list[Image]:
        """The Images Users may pick for a Project, the Base Image first."""
        return [image for image in self.list_all() if image.status == ImageStatus.READY]

    def get(self, image: str) -> Image | None:
        if image == BASE_IMAGE:
            return BASE
        with self._db.transaction() as conn:
            row = conn.execute(f"SELECT {_COLUMNS} FROM images WHERE id = ?", (image,)).fetchone()
        return self._shown(row) if row else None

    def used_by(self, image: str) -> list[UsingProject]:
        """The Projects that use the Image, of every User."""
        with self._db.transaction() as conn:
            return _projects_using(conn, image)

    def delete(self, image: str) -> bool:
        """Delete the Image, built or not. False if not found.

        Raises BaseImageUndeletable, ImageInUse, RuntimeUnavailable.
        """
        if image == BASE_IMAGE:
            raise BaseImageUndeletable
        self._runtime.check_reachable()  # nothing is deleted while Docker cannot remove it
        with self._db.transaction() as conn:
            if using := _projects_using(conn, image):
                raise ImageInUse(using)
            deleted = conn.execute("DELETE FROM images WHERE id = ?", (image,)).rowcount
        if deleted:
            self._remove_built(image)
        return deleted > 0

    def resume(self) -> None:
        """Build again the Images whose build was in progress when Dockyard stopped."""
        with self._db.transaction() as conn:
            rows = conn.execute(
                "SELECT id, instructions FROM images WHERE status = ?", (ImageStatus.BUILDING,)
            ).fetchall()
        for row in rows:
            self._build_in_background(row["id"], row["instructions"])

    def _build_in_background(self, image: str, instructions: str) -> None:
        with self._lock:
            self._logs[image] = ""
        threading.Thread(
            target=self._build, args=(image, instructions), name=f"image-{image}", daemon=True
        ).start()

    def _build(self, image: str, instructions: str) -> None:
        """Build the Image, check it, and record how it went."""

        def output(text: str) -> None:
            with self._lock:
                self._logs[image] = _tail(self._logs.get(image, "") + text)

        failure: Failure | None = Failure.BUILD
        try:
            if self._runtime.build_image(image, instructions, output=output):
                check = self._runtime.check_image(image)
                output(_checked(check))
                failure = _failure(check)
                if failure is not None:  # never picked: nothing keeps it
                    self._remove_built(image)
        except (RuntimeUnavailable, ImageMissing) as error:
            output(f"{error}\n")
        except Exception:  # never left building forever
            log.exception("Building Image %s failed", image)
            output("Dockyard failed to build this Image: its log says why.\n")
        with self._lock:
            printed = self._logs.pop(image, "")
        with self._db.transaction() as conn:
            recorded = conn.execute(
                "UPDATE images SET status = ?, failure = ?, log = ? WHERE id = ?",
                (ImageStatus.FAILED if failure else ImageStatus.READY, failure, printed, image),
            ).rowcount
        if not recorded:  # deleted meanwhile
            self._remove_built(image)

    def _remove_built(self, image: str) -> None:
        try:
            self._runtime.remove_image(image)
        except RuntimeUnavailable as error:
            log.warning("Cannot remove the built Image %s: %s", image, error)

    def _shown(self, row: sqlite3.Row) -> Image:
        """The Image as it is now: with what its build printed so far, while it is built."""
        image = _image(row)
        with self._lock:
            printed = self._logs.get(image.id)
        return image if printed is None else replace(image, log=printed)


def valid_label(label: str) -> str:
    label = label.strip()
    if not label:
        raise ImageRefused(Refusal.LABEL_MISSING)
    if len(label) > LABEL_MAX_LENGTH or not label.isprintable():
        raise ImageRefused(Refusal.LABEL_INVALID)
    return label


def valid_description(description: str) -> str:
    description = description.replace("\r\n", "\n").strip()  # as a form sends it
    if (
        len(description) > DESCRIPTION_MAX_LENGTH
        or not description.replace("\n", " ").isprintable()
    ):
        raise ImageRefused(Refusal.DESCRIPTION_INVALID)
    return description


def valid_instructions(instructions: str) -> str:
    instructions = instructions.replace("\r\n", "\n").strip()
    if not instructions:
        raise ImageRefused(Refusal.INSTRUCTIONS_MISSING)
    if len(instructions) > INSTRUCTIONS_MAX_LENGTH or "\0" in instructions:
        raise ImageRefused(Refusal.INSTRUCTIONS_INVALID)
    if STARTS_A_STAGE.search(instructions):
        raise ImageRefused(Refusal.INSTRUCTIONS_FROM)
    return instructions


def _failure(check: ImageCheck) -> Failure | None:
    if not check.claude:
        return Failure.CLAUDE_MISSING
    if not check.uid:  # root, or unknown
        return Failure.ROOT_USER
    return None


def _checked(check: ImageCheck) -> str:
    """What the check found, as the end of the build log."""
    uid = "unknown" if check.uid is None else check.uid
    return f"Checked: Claude Code {check.claude or 'missing'}; default user uid {uid}.\n"


def _tail(log: str) -> str:
    return "\n".join(log.split("\n")[-BUILD_LOG_LINES:])


_COLUMNS = "id, label, description, instructions, status, failure, log"


def _projects_using(conn: sqlite3.Connection, image: str) -> list[UsingProject]:
    rows = conn.execute(
        "SELECT name, (SELECT email FROM users WHERE runtime_id = owner) AS owner"
        " FROM projects WHERE image = ? ORDER BY created_at, rowid",
        (image,),
    ).fetchall()
    return [UsingProject(row["name"], row["owner"]) for row in rows]


def _image(row: sqlite3.Row) -> Image:
    return Image(
        id=row["id"],
        label=row["label"],
        description=row["description"],
        instructions=row["instructions"],
        status=ImageStatus(row["status"]),
        log=row["log"],
        failure=Failure(row["failure"]) if row["failure"] else None,
    )
