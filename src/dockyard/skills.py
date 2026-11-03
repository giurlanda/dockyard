"""Skills: what Administrators offer Claude Code in Projects, as a catalog for the Instance.

An Administrator uploads a Skill as a single Markdown file, or as a .zip of a folder with a SKILL.md
and its supporting files; Dockyard reads its name and description from the frontmatter (for a
Markdown file, the name when missing from the file name) and keeps its files as a folder named after
the Skill, with SKILL.md at its root and each file only executable or not. Every User reads a Skill
before picking it: like the rest of a Project, it can read the Project's Git Credential (ADR 0002).
"""

from __future__ import annotations

import io
import re
import sqlite3
import stat
import zipfile
import zlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

import yaml

from .database import Database

UPLOAD_MAX_BYTES = 1024 * 1024
DESCRIPTION_MAX_LENGTH = 1024  # Claude Code's limit
VALID_NAME = re.compile(r"[a-z0-9-]{1,64}")  # Claude Code's rules for skill names
SKILL_FILE = "SKILL.md"
# An uploaded zip, unpacked, stays small: no archive bombs.
ARCHIVE_MAX_ENTRIES = 1000
ARCHIVE_MAX_BYTES = 10 * 1024 * 1024


class Refusal(StrEnum):
    TOO_LARGE = "too_large"
    NOT_MARKDOWN = "not_markdown"
    NOT_TEXT = "not_text"
    FRONTMATTER_INVALID = "frontmatter_invalid"
    NAME_INVALID = "name_invalid"
    DESCRIPTION_MISSING = "description_missing"
    DESCRIPTION_INVALID = "description_invalid"
    SKILL_FILE_MISSING = "skill_file_missing"
    NOT_ONE_SKILL = "not_one_skill"
    NOT_ARCHIVE = "not_archive"
    PATH_UNSAFE = "path_unsafe"
    NOT_REGULAR_FILE = "not_regular_file"
    TOO_MANY_FILES = "too_many_files"
    TOO_LARGE_UNPACKED = "too_large_unpacked"
    PATH_REPEATED = "path_repeated"


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
    executable: bool


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
        """Add the uploaded Markdown file or .zip to the catalog: the Skill, with its files.

        Raises SkillRefused, SkillNameTaken.
        """
        name, description, files = _unpack(upload, file_name)
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


def _unpack(upload: bytes, file_name: str) -> tuple[str, str, bytes]:
    """The name, description and files (as stored) of an upload. Raises SkillRefused."""
    if len(upload) > UPLOAD_MAX_BYTES:
        raise SkillRefused(Refusal.TOO_LARGE)
    path = PurePosixPath(file_name.replace("\\", "/"))
    if path.suffix.lower() == ".md":
        name, description = _name_and_description(upload, path.stem)
        return name, description, _folder(name, {SKILL_FILE: (upload, False)})
    if path.suffix.lower() == ".zip":
        files = _archived(upload)
        name, description = _name_and_description(files[SKILL_FILE][0], None)
        return name, description, _folder(name, files)
    raise SkillRefused(Refusal.NOT_MARKDOWN)


def _archived(upload: bytes) -> dict[str, tuple[bytes, bool]]:
    """The files of an uploaded zip, by path within the Skill's folder, each with its content and
    whether it is executable. Raises SkillRefused."""
    try:
        with zipfile.ZipFile(io.BytesIO(upload)) as archive:
            entries = archive.infolist()
            if len(entries) > ARCHIVE_MAX_ENTRIES:
                raise SkillRefused(Refusal.TOO_MANY_FILES)
            files: dict[str, zipfile.ZipInfo] = {}
            for entry in entries:
                if _directory(entry):
                    continue
                path = _path(entry)
                if path.startswith("__MACOSX/"):  # resource forks the macOS Finder adds
                    continue
                if path in files:
                    raise SkillRefused(Refusal.PATH_REPEATED)
                files[path] = entry
            if any(
                str(folder) in files for path in files for folder in PurePosixPath(path).parents
            ):
                raise SkillRefused(Refusal.PATH_REPEATED)  # a file where a folder is
            if sum(entry.file_size for entry in files.values()) > ARCHIVE_MAX_BYTES:
                # zipfile never unpacks more than an entry's declared size: this bounds it all.
                raise SkillRefused(Refusal.TOO_LARGE_UNPACKED)
            prefix = _skill_folder(list(files))
            return {
                path.removeprefix(prefix): (archive.read(entry), bool(_mode(entry) & 0o111))
                for path, entry in files.items()
            }
    except (zipfile.BadZipFile, zlib.error, EOFError, ValueError, NotImplementedError) as error:
        raise SkillRefused(Refusal.NOT_ARCHIVE) from error


def _path(entry: zipfile.ZipInfo) -> str:
    """The entry's path, refused when it could land outside the folder, or is not a plain file
    (a link, a device) or not a plainly compressed one. Raises SkillRefused."""
    path = entry.filename.replace("\\", "/")
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if not parts or path.startswith("/") or re.match(r"[A-Za-z]:", path) or ".." in parts:
        raise SkillRefused(Refusal.PATH_UNSAFE)
    kind = stat.S_IFMT(_mode(entry))
    if kind not in (0, stat.S_IFREG, stat.S_IFDIR):  # 0: no Unix mode, a plain file or folder
        raise SkillRefused(Refusal.NOT_REGULAR_FILE)
    if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) or entry.flag_bits & 1:
        raise SkillRefused(Refusal.NOT_ARCHIVE)  # other methods, or encrypted
    return "/".join(parts)


def _directory(entry: zipfile.ZipInfo) -> bool:
    return entry.is_dir() or stat.S_ISDIR(_mode(entry))


def _mode(entry: zipfile.ZipInfo) -> int:
    """The entry's Unix mode, 0 when it has none."""
    return entry.external_attr >> 16


def _skill_folder(paths: list[str]) -> str:
    """Where the one SKILL.md sits: "" at the root, "<folder>/" in the only top-level folder.
    Raises SkillRefused."""
    skill_files = [PurePosixPath(path) for path in paths if PurePosixPath(path).name == SKILL_FILE]
    if len(skill_files) > 1:
        raise SkillRefused(Refusal.NOT_ONE_SKILL)
    if not skill_files or len(skill_files[0].parts) > 2:  # neither at the root nor one folder in
        raise SkillRefused(Refusal.SKILL_FILE_MISSING)
    prefix = "" if len(skill_files[0].parts) == 1 else f"{skill_files[0].parts[0]}/"
    if not all(path.startswith(prefix) for path in paths):
        raise SkillRefused(Refusal.NOT_ONE_SKILL)
    return prefix


def _name_and_description(skill_file: bytes, file_stem: str | None) -> tuple[str, str]:
    """The name and description of a SKILL.md: the name, when missing, from the file's stem (if
    any). Raises SkillRefused."""
    try:
        text = skill_file.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SkillRefused(Refusal.NOT_TEXT) from error
    frontmatter = _frontmatter(text)
    name = frontmatter.get("name") or file_stem
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


def _folder(name: str, files: dict[str, tuple[bytes, bool]]) -> bytes:
    """The Skill's files as stored: a zip of its folder, files only, each regular and either
    executable or not."""
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as folder:
        for path, (content, executable) in files.items():
            entry = zipfile.ZipInfo(f"{name}/{path}")
            entry.create_system = 3  # Unix: the mode lives in the high 16 bits
            entry.external_attr = (0o100755 if executable else 0o100644) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            folder.writestr(entry, content)
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
            executable = bool(_mode(entry) & 0o111)
            files.append(SkillFile(path, len(data), content, executable))
    return tuple(files)


_COLUMNS = "id, name, description, updated_at"


def _skill(row: sqlite3.Row, files: tuple[SkillFile, ...] = ()) -> Skill:
    return Skill(row["id"], row["name"], row["description"], row["updated_at"], files)
