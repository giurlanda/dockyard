"""Skills uploaded as a .zip: a folder with a SKILL.md and its supporting files. Dockyard keeps them
as one folder named after the Skill, and refuses an archive that is malformed or unsafe."""

import io
import random
import zipfile
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dockyard.skills import ARCHIVE_MAX_BYTES, ARCHIVE_MAX_ENTRIES

from .conftest import ADMINISTRATOR, AccessProxy

SKILL = b"""---
name: release
description: Cut a release with the team's script.
---

Run `scripts/release.sh`, then follow `reference.md`.
"""
SCRIPT = b"#!/bin/sh\necho released\n"


@pytest.fixture
def administrator(proxy: AccessProxy) -> dict[str, str]:
    return proxy.headers(ADMINISTRATOR)


def archive(*entries: tuple[str | zipfile.ZipInfo, bytes]) -> bytes:
    """A zip built here: each entry a path (or a ZipInfo, for its mode) and its content."""
    built = io.BytesIO()
    with zipfile.ZipFile(built, "w", zipfile.ZIP_DEFLATED) as folder:
        for entry, content in entries:
            folder.writestr(entry, content)
    return built.getvalue()


def with_mode(path: str, mode: int) -> zipfile.ZipInfo:
    entry = zipfile.ZipInfo(path)
    entry.create_system = 3  # Unix: the mode lives in the high 16 bits
    entry.external_attr = mode << 16
    return entry


def compressed(path: str, compress_type: int) -> zipfile.ZipInfo:
    entry = zipfile.ZipInfo(path)
    entry.compress_type = compress_type
    return entry


def encrypted(built: bytes) -> bytes:
    """The archive with its last entry marked as encrypted (zipfile never writes one)."""
    flags = built.rindex(b"PK\x01\x02") + 8  # in the central directory, which zipfile reads
    return built[:flags] + bytes([built[flags] | 1]) + built[flags + 1 :]


def upload(dockyard: TestClient, headers: dict[str, str], content: bytes) -> Any:
    files = {"file": ("release.zip", content, "application/zip")}
    return dockyard.post("/api/skills", files=files, headers=headers)


def test_a_zip_with_skill_md_at_its_root_becomes_a_skill_named_from_the_frontmatter(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    uploaded = upload(
        dockyard, administrator, archive(("SKILL.md", SKILL), ("reference.md", b"# Steps\n"))
    )

    assert uploaded.status_code == 201
    assert uploaded.json()["name"] == "release"
    assert uploaded.json()["description"] == "Cut a release with the team's script."
    assert {file["path"] for file in uploaded.json()["files"]} == {"SKILL.md", "reference.md"}


def test_a_zip_with_skill_md_inside_a_single_top_level_folder_becomes_a_skill(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    content = archive(
        ("shipping/", b""),
        ("shipping/SKILL.md", SKILL),
        ("shipping/scripts/release.sh", SCRIPT),
    )

    uploaded = upload(dockyard, administrator, content)

    assert uploaded.status_code == 201
    assert uploaded.json()["name"] == "release"
    read = dockyard.get(f"/api/skills/{uploaded.json()['id']}", headers=administrator).json()
    assert {file["path"]: file["content"] for file in read["files"]} == {
        "SKILL.md": SKILL.decode(),
        "scripts/release.sh": SCRIPT.decode(),
    }


def test_executable_bits_survive_and_other_modes_are_not_kept(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    content = archive(
        (with_mode("SKILL.md", 0o100600), SKILL),
        (with_mode("scripts/release.sh", 0o100750), SCRIPT),
        (with_mode("reference.md", 0o104666), b"# Steps\n"),  # setuid, world-writable
    )

    skill = upload(dockyard, administrator, content).json()["id"]

    read = dockyard.get(f"/api/skills/{skill}", headers=administrator).json()
    assert {file["path"]: file["executable"] for file in read["files"]} == {
        "SKILL.md": False,
        "scripts/release.sh": True,
        "reference.md": False,
    }


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"PK\x03\x04 not really a zip", "not_archive"),
        (archive(("README.md", b"# No skill here\n")), "skill_file_missing"),
        (archive(("a/b/SKILL.md", SKILL)), "skill_file_missing"),
        (archive(("SKILL.md", SKILL), ("other/SKILL.md", SKILL)), "not_one_skill"),
        (archive(("SKILL.md", SKILL), ("./SKILL.md", SKILL)), "path_repeated"),
        (archive(("SKILL.md", SKILL), ("notes", b""), ("notes/a.md", b"")), "path_repeated"),
        (archive(("SKILL.md", SKILL), (".", b"")), "path_unsafe"),
        (archive(("release/SKILL.md", SKILL), ("other/notes.md", b"")), "not_one_skill"),
        (archive(("release/SKILL.md", SKILL), ("notes.md", b"")), "not_one_skill"),
        (archive(("SKILL.md", SKILL), ("/etc/cron.d/evil", b"")), "path_unsafe"),
        (archive(("SKILL.md", SKILL), ("C:/evil.bat", b"")), "path_unsafe"),
        (archive(("SKILL.md", SKILL), ("../evil.sh", b"")), "path_unsafe"),
        (archive(("SKILL.md", SKILL), ("scripts/..\\..\\evil.sh", b"")), "path_unsafe"),
        (
            archive(("SKILL.md", SKILL), (with_mode("link", 0o120777), b"/etc/passwd")),
            "not_regular_file",
        ),
        (archive(("SKILL.md", SKILL), (with_mode("pipe", 0o010644), b"")), "not_regular_file"),
        (encrypted(archive(("SKILL.md", SKILL), ("a.md", b""))), "not_archive"),
        (archive(("SKILL.md", SKILL), (compressed("a.md", zipfile.ZIP_LZMA), b"")), "not_archive"),
        (archive(("SKILL.md", SKILL))[:-30], "not_archive"),  # cut short
        (
            archive(("SKILL.md", SKILL), *((f"{n}.md", b"") for n in range(ARCHIVE_MAX_ENTRIES))),
            "too_many_files",
        ),
        (archive(("SKILL.md", SKILL), ("bomb", bytes(ARCHIVE_MAX_BYTES))), "too_large_unpacked"),
        # The same rules as for a single Markdown file.
        (archive(("SKILL.md", b"---\ndescription: No name.\n---\n")), "name_invalid"),
        (archive(("SKILL.md", b"---\nname: release\n---\n")), "description_missing"),
        (
            archive(("SKILL.md", SKILL), ("noise", random.Random(0).randbytes(1024 * 1024))),
            "too_large",
        ),
    ],
)
def test_a_malformed_or_unsafe_zip_is_refused_with_a_reason(
    dockyard: TestClient, administrator: dict[str, str], content: bytes, reason: str
) -> None:
    refused = upload(dockyard, administrator, content)

    assert refused.status_code == 422
    assert refused.json()["error"] == reason
    assert refused.json()["message"]
    assert dockyard.get("/api/skills", headers=administrator).json() == []


def test_the_skill_page_shows_text_files_and_lists_the_others_by_path_and_size(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    content = archive(
        ("release/SKILL.md", SKILL),
        ("release/scripts/release.sh", SCRIPT),
        ("release/assets/logo.png", b"\x89PNG\r\n\x1a\n\xff\xfe"),
    )
    files = {"file": ("release.zip", content, "application/zip")}

    created = dockyard.post("/skills", files=files, headers=administrator, follow_redirects=False)

    page = dockyard.get(created.headers["location"], headers=administrator).text
    assert "scripts/release.sh" in page
    assert "echo released" in page
    assert "assets/logo.png" in page
    assert "Not text: 10 bytes." in page


def test_a_refused_zip_says_why_on_the_page(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    files = {"file": ("evil.zip", archive(("SKILL.md", SKILL), ("../x", b"")), "application/zip")}

    refused = dockyard.post("/skills", files=files, headers=administrator)

    assert refused.status_code == 422
    assert "climbs out of the folder" in refused.text


def test_a_zip_made_by_the_macos_finder_leaves_its_resource_forks_behind(
    dockyard: TestClient, administrator: dict[str, str]
) -> None:
    content = archive(
        ("release/SKILL.md", SKILL),
        ("__MACOSX/release/._SKILL.md", b"\x00\x05\x16\x07"),
    )

    uploaded = upload(dockyard, administrator, content)

    assert uploaded.status_code == 201
    assert [file["path"] for file in uploaded.json()["files"]] == ["SKILL.md"]
