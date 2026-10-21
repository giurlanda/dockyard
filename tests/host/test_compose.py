"""The three installations the README describes, as docker compose sees them from .env.

Only `docker compose config` runs: nothing starts, so these tests need Docker's compose plugin
and nothing else.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from .conftest import compose, install

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0,
    reason="needs Docker's compose plugin",
)

REQUIRED = {
    "DOCKYARD_SECRET_KEY": "x" * 43 + "=",
    "DOCKYARD_FIRST_ADMINISTRATOR_EMAIL": "admin@example.com",
}
CLOUDFLARE = {
    "DOCKYARD_CF_ACCESS_TEAM": "acme",
    "DOCKYARD_CF_ACCESS_AUD": "aud-tag",
    "DOCKYARD_CF_TUNNEL_TOKEN": "tunnel-token",
}
LOCAL_ONLY = {
    "COMPOSE_FILE": "compose.yaml:compose.local.yaml",
    "DOCKYARD_LOCAL_ADDRESS": "192.168.1.10",
}
BOTH = {
    **CLOUDFLARE,
    **LOCAL_ONLY,
    "COMPOSE_FILE": "compose.yaml:compose.cloudflare.yaml:compose.local.yaml",
}
# As the README shows it: the local port on a Tailscale address too.
VPN_OVERRIDE = """\
services:
  dockyard:
    ports:
      - "100.101.102.103:${DOCKYARD_LOCAL_HOST_PORT:-8443}:8443"
"""


def services(directory: Path) -> dict[str, Any]:
    """The services docker compose would start, with their settings."""
    result = compose(directory, "config", "--format", "json")
    assert result.returncode == 0, result.stderr
    found: dict[str, Any] = json.loads(result.stdout)["services"]
    return found


def published(service: dict[str, Any]) -> list[tuple[str, str]]:
    return [(port.get("host_ip", ""), port["published"]) for port in service.get("ports", [])]


def test_a_fresh_env_with_cloudflare_filled_in_runs_the_tunnel_and_publishes_no_port(
    tmp_path: Path,
) -> None:
    found = services(install(tmp_path, {**REQUIRED, **CLOUDFLARE}))

    assert set(found) == {"socket-proxy", "dockyard", "tunnel", "base-image"}
    assert found["tunnel"]["environment"]["TUNNEL_TOKEN"] == "tunnel-token"
    assert all(published(service) == [] for service in found.values())
    assert "DOCKYARD_LOCAL_PORT" not in found["dockyard"]["environment"]


def test_with_cloudflare_on_the_tunnel_token_is_required(tmp_path: Path) -> None:
    directory = install(tmp_path, {**REQUIRED, **CLOUDFLARE, "DOCKYARD_CF_TUNNEL_TOKEN": ""})

    result = compose(directory, "config")

    assert result.returncode != 0
    assert "DOCKYARD_CF_TUNNEL_TOKEN" in result.stderr


def test_local_sign_in_only_runs_no_tunnel_and_asks_no_cloudflare_setting(
    tmp_path: Path,
) -> None:
    found = services(install(tmp_path, {**REQUIRED, **LOCAL_ONLY}))

    assert "tunnel" not in found
    assert published(found["dockyard"]) == [("192.168.1.10", "8443")]
    assert found["dockyard"]["environment"]["DOCKYARD_LOCAL_PORT"] == "8443"


def test_local_sign_in_needs_the_host_address_named(tmp_path: Path) -> None:
    directory = install(tmp_path, {**REQUIRED, **LOCAL_ONLY, "DOCKYARD_LOCAL_ADDRESS": ""})

    result = compose(directory, "config")

    assert result.returncode != 0
    assert "DOCKYARD_LOCAL_ADDRESS" in result.stderr


def test_both_ways_in_run_the_tunnel_and_publish_the_local_port(tmp_path: Path) -> None:
    found = services(install(tmp_path, {**REQUIRED, **BOTH, "DOCKYARD_LOCAL_HOST_PORT": "9443"}))

    assert found["tunnel"]["environment"]["TUNNEL_TOKEN"] == "tunnel-token"
    assert published(found["dockyard"]) == [("192.168.1.10", "9443")]


def test_compose_override_publishes_the_local_port_on_a_vpn_address_too(
    tmp_path: Path,
) -> None:
    with_override = f"{LOCAL_ONLY['COMPOSE_FILE']}:compose.override.yaml"
    directory = install(tmp_path, {**REQUIRED, **LOCAL_ONLY, "COMPOSE_FILE": with_override})
    (directory / "compose.override.yaml").write_text(VPN_OVERRIDE)

    found = services(directory)

    assert sorted(published(found["dockyard"])) == [
        ("100.101.102.103", "8443"),
        ("192.168.1.10", "8443"),
    ]
