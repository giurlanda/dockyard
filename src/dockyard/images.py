"""Images: the Base Image, and the Images Administrators define on top of it.

An Administrator writes only an Image's extra build instructions: the Runtime builds them on top of
the Base Image, as root, then back to its non-root user. A built Image is checked (Claude Code is
there, its default user is not root) before Users may pick it for a Project.

Each build of an Image has its own name. Projects start from the last build that passed its check:
one running an older build keeps it until it starts again, and a build no Project starts from any
more is removed once none runs it. When the Base Image changes, every Image is built again.
"""

from __future__ import annotations

import logging
import re
import secrets
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
# Seconds between two looks for a new Base Image, and for builds no Project runs any more.
CHECK_INTERVAL = 60.0
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


class BaseImageNotBuilt(Exception):
    """The Base Image is shipped with Dockyard: Dockyard never builds it."""


class ImageBuilding(Exception):
    """The Image is being built already."""


@dataclass(frozen=True)
class Image:
    id: str
    label: str
    description: str
    instructions: str  # the extra build instructions, "" for the Base Image
    status: ImageStatus
    log: str = ""  # what its build printed, then what its check found
    failure: Failure | None = None
    # The build Projects start from: the last one that passed its check, None until one did.
    # A failed build leaves it as it was.
    build: str | None = None

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
        self._closed = threading.Event()

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
        """The Images Users may pick for a Project, the Base Image first: those with a build
        that passed its check, even while they are built again or after that failed."""
        return [image for image in self.list_all() if image.base or image.build]

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
            deleted = conn.execute(
                "DELETE FROM images WHERE id = ? RETURNING build", (image,)
            ).fetchone()
        if deleted is None:
            return False
        if deleted["build"]:  # a build in progress is removed when it ends
            self._retire(deleted["build"])
        return True

    def rebuild(self, image: str) -> Image | None:
        """Build the Image again; until its new build passes its check, Projects keep starting
        from the build they had. None if not found.

        Raises BaseImageNotBuilt, ImageBuilding, RuntimeUnavailable.
        """
        if image == BASE_IMAGE:
            raise BaseImageNotBuilt
        if self.get(image) is None:
            return None
        self._runtime.check_reachable()  # a build Docker cannot start would fail for good
        with self._db.transaction() as conn:
            row = conn.execute(
                "UPDATE images SET status = ? WHERE id = ? AND status != ? RETURNING instructions",
                (ImageStatus.BUILDING, image, ImageStatus.BUILDING),
            ).fetchone()
        if row is None:
            if self.get(image) is None:  # deleted meanwhile
                return None
            raise ImageBuilding
        self._build_in_background(image, row["instructions"])
        return self.get(image)

    def failed(self) -> list[Image]:
        """The Images whose last build failed: Administrators see them highlighted."""
        return [image for image in self.list_all() if image.status == ImageStatus.FAILED]

    def resume(self) -> None:
        """Build again the Images whose build was in progress when Dockyard stopped, and those
        built on an older Base Image; remove the builds no Project runs any more."""
        with self._db.transaction() as conn:
            rows = conn.execute(
                "SELECT id, instructions FROM images WHERE status = ?", (ImageStatus.BUILDING,)
            ).fetchall()
        for row in rows:
            self._build_in_background(row["id"], row["instructions"])
        self._keep_current()

    def keep_current(self) -> None:
        """Until close: build every Image again when the Base Image changes, as when a newer
        release of it is pulled, and remove the builds no Project runs any more."""
        threading.Thread(target=self._keep_current_until_closed, name="images", daemon=True).start()

    def close(self) -> None:
        """Dockyard is stopping: stop keeping the Images current."""
        self._closed.set()

    def _keep_current_until_closed(self) -> None:
        while not self._closed.wait(CHECK_INTERVAL):
            self._keep_current()

    def _keep_current(self) -> None:
        try:
            self._rebuild_on_new_base()
            self._remove_retired()
        except RuntimeUnavailable as error:
            log.warning("Cannot keep the Images current: %s", error)
        except Exception:  # never the end of it: the next look may succeed
            log.exception("Cannot keep the Images current")

    def _rebuild_on_new_base(self) -> None:
        """Build again every Image whose last build started from another Base Image than the
        one there is now. Raises RuntimeUnavailable."""
        base = self._runtime.base_build()
        with self._db.transaction() as conn:
            rows = conn.execute(
                "UPDATE images SET status = ? WHERE status != ? AND base_build IS NOT ?"
                " RETURNING id, instructions",
                (ImageStatus.BUILDING, ImageStatus.BUILDING, base),
            ).fetchall()
        for row in rows:
            log.info("The Base Image changed: building Image %s again", row["id"])
            self._build_in_background(row["id"], row["instructions"])

    def _retire(self, build: str) -> None:
        """No Project starts from the build any more: remove it once none runs it."""
        with self._db.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO retired_builds (build) VALUES (?)", (build,))
        try:
            self._remove_retired()
        except RuntimeUnavailable as error:
            log.warning("Cannot remove the builds no Project runs yet: %s", error)

    def _remove_retired(self) -> None:
        """Remove the retired builds no Project runs any more. Raises RuntimeUnavailable."""
        with self._db.transaction() as conn:
            builds = [row["build"] for row in conn.execute("SELECT build FROM retired_builds")]
        for build in builds:
            if self._runtime.remove_image(build):
                with self._db.transaction() as conn:
                    conn.execute("DELETE FROM retired_builds WHERE build = ?", (build,))

    def _build_in_background(self, image: str, instructions: str) -> None:
        with self._lock:
            self._logs[image] = ""
        build = f"{image}-{secrets.token_hex(4)}"
        threading.Thread(
            target=self._build,
            args=(image, build, instructions),
            name=f"image-{image}",
            daemon=True,
        ).start()

    def _build(self, image: str, build: str, instructions: str) -> None:
        """Build the Image anew as `build`, check it, and record how it went: Projects start
        from it once it passed its check."""

        def output(text: str) -> None:
            with self._lock:
                self._logs[image] = _tail(self._logs.get(image, "") + text)

        failure: Failure | None = Failure.BUILD
        base: str | None = None
        try:
            base = self._runtime.base_build()  # before the build: it may change meanwhile
            if self._runtime.build_image(build, instructions, output=output):
                check = self._runtime.check_image(build)
                output(_checked(check))
                failure = _failure(check)
        except (RuntimeUnavailable, ImageMissing) as error:
            output(f"{error}\n")
        except Exception:  # never left building forever
            log.exception("Building Image %s failed", image)
            output("Dockyard failed to build this Image: its log says why.\n")
        with self._lock:
            printed = self._logs.pop(image, "")
        with self._db.transaction() as conn:
            previous = conn.execute("SELECT build FROM images WHERE id = ?", (image,)).fetchone()
            conn.execute(
                "UPDATE images SET status = ?, failure = ?, log = ?, base_build = ?,"
                " build = CASE WHEN ? IS NULL THEN ? ELSE build END WHERE id = ?",
                (
                    ImageStatus.FAILED if failure else ImageStatus.READY,
                    failure,
                    printed,
                    base,
                    failure,
                    build,
                    image,
                ),
            )
        if previous is None or failure is not None:  # deleted meanwhile, or never started from
            self._retire(build)
        elif previous["build"]:
            self._retire(previous["build"])

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


_COLUMNS = "id, label, description, instructions, status, failure, log, build"


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
        build=row["build"],
    )
