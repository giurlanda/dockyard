"""The Git hosting providers Dockyard works with."""

from __future__ import annotations

from enum import StrEnum


class Provider(StrEnum):
    GITHUB = "github"
    GITLAB = "gitlab"
