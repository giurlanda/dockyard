"""An installation as the README makes one: the compose files and a copy of .env.example."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[2]
COMPOSE_FILES = ("compose.yaml", "compose.local.yaml")


def install(directory: Path, settings: dict[str, str]) -> Path:
    """The compose files in `directory`, and a .env copied from .env.example with `settings`
    filled in. Each setting must be in .env.example, set or commented out."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in COMPOSE_FILES:
        shutil.copy(REPO / name, directory / name)
    lines = (REPO / ".env.example").read_text().splitlines()
    for name, value in settings.items():
        found = [
            index
            for index, line in enumerate(lines)
            if line.removeprefix("# ").startswith(f"{name}=")
        ]
        assert found, f"{name} is not in .env.example"
        lines[found[0]] = f"{name}={value}"
    (directory / ".env").write_text("\n".join(lines) + "\n")
    return directory


def compose(directory: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """docker compose in `directory`, with only its .env: none of this shell's settings."""
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("COMPOSE_", "DOCKYARD_"))
    }
    return subprocess.run(
        ["docker", "compose", *arguments],
        cwd=directory,
        env=env,
        capture_output=True,
        text=True,
    )
