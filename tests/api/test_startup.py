"""Dockyard refuses to start on an incomplete configuration or when Docker is unreachable."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import instance_env

ESSENTIAL = [
    "DOCKYARD_SECRET_KEY",
    "DOCKYARD_CF_ACCESS_TEAM",
    "DOCKYARD_CF_ACCESS_AUD",
    "DOCKYARD_FIRST_ADMINISTRATOR_EMAIL",
]
UNREACHABLE_DOCKER = "tcp://127.0.0.1:9"


def start_dockyard(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "dockyard"],
        env={"PATH": os.environ["PATH"], "DOCKER_HOST": UNREACHABLE_DOCKER, **env},
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("missing", ESSENTIAL)
def test_dockyard_refuses_to_start_without_an_essential_setting(
    tmp_path: Path, missing: str
) -> None:
    env = instance_env(tmp_path)
    del env[missing]

    result = start_dockyard(env)

    assert result.returncode != 0
    assert missing in result.stderr
    assert "configuration" in result.stderr


def test_every_missing_setting_is_reported_at_once() -> None:
    result = start_dockyard({})

    assert result.returncode != 0
    for name in ESSENTIAL:
        assert name in result.stderr


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("DOCKYARD_SECRET_KEY", "too-short"),
        ("DOCKYARD_FIRST_ADMINISTRATOR_EMAIL", "not-an-email"),
        ("DOCKYARD_CF_ACCESS_TEAM", "https://acme.cloudflareaccess.com"),
        ("DOCKYARD_DEV_IDENTITY", "maybe"),
        ("DOCKYARD_LISTEN_PORT", "eighty"),
        ("DOCKYARD_LISTEN_PORT", "70000"),
        ("DOCKYARD_PROJECTS_DNS", "dns.example.com"),
        ("DOCKYARD_PROJECTS_DNS", "1.1.1.1;9.9.9.9"),
        ("DOCKYARD_PROJECTS_DNS", ","),
        ("DOCKYARD_PROJECT_MAX_CPUS", "two"),
        ("DOCKYARD_PROJECT_MAX_CPUS", "0.25"),
        ("DOCKYARD_PROJECT_MAX_CPUS", "inf"),
        ("DOCKYARD_PROJECT_MAX_MEMORY_GIB", "4g"),
        ("DOCKYARD_PROJECT_MAX_MEMORY_GIB", "0.5"),
        ("DOCKYARD_MAX_ACTIVE_PROJECTS", "0"),
        ("DOCKYARD_MAX_ACTIVE_PROJECTS", "2.5"),
        ("DOCKYARD_MAX_ACTIVE_PROJECTS", "nan"),
    ],
)
def test_dockyard_refuses_to_start_with_an_invalid_setting(
    tmp_path: Path, setting: str, value: str
) -> None:
    result = start_dockyard({**instance_env(tmp_path), setting: value})

    assert result.returncode != 0
    assert setting in result.stderr


def test_the_development_identity_does_not_need_proxy_settings_to_start(tmp_path: Path) -> None:
    env = instance_env(tmp_path)
    del env["DOCKYARD_CF_ACCESS_TEAM"], env["DOCKYARD_CF_ACCESS_AUD"]

    result = start_dockyard({**env, "DOCKYARD_DEV_IDENTITY": "true"})

    assert "configuration" not in result.stderr
    assert UNREACHABLE_DOCKER in result.stderr  # it got as far as looking for Docker


def test_dockyard_refuses_to_start_when_docker_is_unreachable(tmp_path: Path) -> None:
    result = start_dockyard(instance_env(tmp_path))

    assert result.returncode != 0
    assert UNREACHABLE_DOCKER in result.stderr
    assert "socket proxy" in result.stderr
