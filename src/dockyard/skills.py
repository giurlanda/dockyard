"""Skills: what Administrators offer Claude Code in Projects, as a catalog for the Instance.

An Administrator uploads a Skill as a single Markdown file; Dockyard reads its name and description
from the file's frontmatter (the name, when missing, from the file name) and keeps its files as a
folder named after the Skill, with the file as its SKILL.md. Every User reads a Skill before picking
it: like the rest of a Project, it can read the Project's Git Credential (ADR 0002).
"""

from __future__ import annotations

import io
import re
import sqlite3
import zipfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

import yaml

from .database import Database

UPLOAD_MAX_BYTES = 1024 * 1024
DESCRIPTION_MAX_LENGTH = 1024  # Claude Code's limit
VALID_NAME = re.compile(r"[a-z0-9-]{1,64}")  # Claude Code's rules for skill names
SKILL_FILE = "SKILL.md"


class Refusal(StrEnum):
    TOO_LARGE = "too_large"
    NOT_MARKDOWN = "not_markdown"
    NOT_TEXT = "not_text"
    FRONTMATTER_INVALID = "frontmatter_invalid"
    NAME_INVALID = "name_invalid"
    DESCRIPTION_MISSING = "description_missing"
    DESCRIPTION_INVALID = "description_invalid"


class SkillRefused(Exception):
    def __init__(self, reason: Refusal) -> None:
        self.reason = reason
        super().__init__(reason)


class SkillNameTaken(Exception):
    """Another Skill has the name: `/name` must mean one Skill on the Instance."""


@dataclass(frozen=True)
class SkillFile:
    path: str  # within the Skill's folder
    size: int
    content: str | None  # None when it is not UTF-8 text


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    description: str
    updated_at: str
    files: tuple[SkillFile, ...] = ()  # only when got on its own


class Skills:
    def __init__(self, database: Database) -> None:
        self._db = database

    def create(self, upload: bytes, file_name: str) -> Skill:
        """Add the uploaded Markdown file to the catalog: the Skill, with its files.

        Raises SkillRefused, SkillNameTaken.
        """
        name, description = _name_and_description(upload, file_name)
        files = _folder(name, upload)
        try:
            with self._db.transaction() as conn:
                row = conn.execute(
                    "INSERT INTO skills (name, description, files) VALUES (?, ?, ?)"
                    f" RETURNING {_COLUMNS}",
                    (name, description, files),
                ).fetchone()
        except sqlite3.IntegrityError as error:
            raise SkillNameTaken from error
        return _skill(row, files=_files(files))

    def list_all(self) -> list[Skill]:
        with self._db.transaction() as conn:
            rows = conn.execute(f"SELECT {_COLUMNS} FROM skills ORDER BY name").fetchall()
        return [_skill(row) for row in rows]

    def get(self, skill: str) -> Skill | None:
        """The Skill with its files."""
        with self._db.transaction() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS}, files FROM skills WHERE id = ?", (skill,)
            ).fetchone()
        return _skill(row, files=_files(row["files"])) if row else None

    def delete(self, skill: str) -> bool:
        """False if not found."""
        with self._db.transaction() as conn:
            return conn.execute("DELETE FROM skills WHERE id = ?", (skill,)).rowcount > 0


def _name_and_description(upload: bytes, file_name: str) -> tuple[str, str]:
    """The name and description of an uploaded Markdown file. Raises SkillRefused."""
    if len(upload) > UPLOAD_MAX_BYTES:
        raise SkillRefused(Refusal.TOO_LARGE)
    path = PurePosixPath(file_name.replace("\\", "/"))
    if path.suffix.lower() != ".md":
        raise SkillRefused(Refusal.NOT_MARKDOWN)
    try:
        text = upload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SkillRefused(Refusal.NOT_TEXT) from error
    frontmatter = _frontmatter(text)
    name = frontmatter.get("name") or path.stem
    if not isinstance(name, str) or not VALID_NAME.fullmatch(name):
        raise SkillRefused(Refusal.NAME_INVALID)
    description = frontmatter.get("description")
    if description is None or (isinstance(description, str) and not description.strip()):
        raise SkillRefused(Refusal.DESCRIPTION_MISSING)
    if not isinstance(description, str) or len(description.strip()) > DESCRIPTION_MAX_LENGTH:
        raise SkillRefused(Refusal.DESCRIPTION_INVALID)
    return name, description.strip()


def _frontmatter(text: str) -> dict[object, object]:
    """The YAML between the first two `---` lines, {} when the file does not start with one."""
    lines = text.removeprefix("﻿").splitlines()
    if not lines or lines[0].rstrip() != "---":
        return {}
    for end, line in enumerate(lines[1:], start=1):
        if line.rstrip() == "---":
            try:
                found = yaml.safe_load("\n".join(lines[1:end]))
            except yaml.YAMLError as error:
                raise SkillRefused(Refusal.FRONTMATTER_INVALID) from error
            if found is None:
                return {}
            if not isinstance(found, dict):
                raise SkillRefused(Refusal.FRONTMATTER_INVALID)
            return found
    raise SkillRefused(Refusal.FRONTMATTER_INVALID)  # never closed


def _folder(name: str, skill_file: bytes) -> bytes:
    """The Skill's files as stored: a zip of its folder, the Markdown file as its SKILL.md."""
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as folder:
        folder.writestr(f"{name}/{SKILL_FILE}", skill_file)
    return archive.getvalue()


def _files(stored: bytes) -> tuple[SkillFile, ...]:
    files = []
    with zipfile.ZipFile(io.BytesIO(stored)) as folder:
        for entry in folder.infolist():
            data = folder.read(entry)
            try:
                content: str | None = data.decode("utf-8")
            except UnicodeDecodeError:
                content = None
            path = entry.filename.split("/", 1)[1]  # within the Skill's folder
            files.append(SkillFile(path, len(data), content))
    return tuple(files)


_COLUMNS = "id, name, description, updated_at"


def _skill(row: sqlite3.Row, files: tuple[SkillFile, ...] = ()) -> Skill:
    return Skill(row["id"], row["name"], row["description"], row["updated_at"], files)
