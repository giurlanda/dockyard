"""UI language: every string the UI shows lives in one catalogue per language."""

from __future__ import annotations

import json
from collections.abc import Callable
from enum import StrEnum
from importlib.resources import files

from starlette.requests import HTTPConnection


class Language(StrEnum):
    EN = "en"
    IT = "it"


DEFAULT_LANGUAGE = Language.EN
LANGUAGE_NAMES = {Language.EN: "English", Language.IT: "Italiano"}  # each in its own language
LANGUAGE_COOKIE = "dockyard_lang"

Translate = Callable[..., str]


def _load(language: Language) -> dict[str, str]:
    catalogue: dict[str, str] = json.loads(
        files(__package__).joinpath("locales", f"{language}.json").read_text("utf-8")
    )
    return catalogue


def _complete(catalogues: dict[Language, dict[str, str]]) -> dict[Language, dict[str, str]]:
    every_key = set().union(*catalogues.values())
    for language, catalogue in catalogues.items():
        if missing := sorted(every_key - catalogue.keys()):
            raise RuntimeError(f"UI strings missing from the {language} catalogue: {missing}")
    return catalogues


CATALOGUES = _complete({language: _load(language) for language in Language})


def language_for(conn: HTTPConnection) -> Language:
    """The language the person chose, else the best one their browser asks for."""
    chosen = conn.cookies.get(LANGUAGE_COOKIE, "")
    if chosen in Language:
        return Language(chosen)
    return _from_accept_language(conn.headers.get("accept-language", ""))


def _from_accept_language(header: str) -> Language:
    ranked: list[tuple[float, int, Language]] = []
    for position, entry in enumerate(header.split(",")):
        tag, *params = (part.strip() for part in entry.split(";"))
        quality = 1.0
        for param in params:
            name, _, value = param.partition("=")
            if name.strip() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        primary = tag.lower().split("-")[0]
        if quality > 0 and primary in Language:
            ranked.append((-quality, position, Language(primary)))
    return min(ranked)[2] if ranked else DEFAULT_LANGUAGE


def translator(language: Language) -> Translate:
    catalogue = CATALOGUES[language]

    def translate(key: str, **params: object) -> str:
        return catalogue[key].format(**params)

    return translate
