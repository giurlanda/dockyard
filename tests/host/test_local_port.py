"""The local port answers only on the host IPv4 address named in .env.

These tests add a network interface to the host and run Dockyard, built from this checkout, with
compose.local.yaml, so they run only as root on a Linux host with Docker, and only when
DOCKYARD_LOCAL_PORT_TESTS=1 (CI sets it). A dummy interface gives the host two more IPv4
addresses, one of them named in .env, and an IPv6 one.
"""

from __future__ import annotations

import http.client
import os
import platform
import ssl
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from .conftest import REPO, compose, install

INTERFACE = "dy-local"
NAMED = "192.168.251.10"  # DOCKYARD_LOCAL_ADDRESS
OTHER = "192.168.251.11"  # another address of the same host
IPV6 = "fd00:d0c:1::10"
PORT = 8443  # the default of DOCKYARD_LOCAL_HOST_PORT
VERSION = "host-test"  # the tag this checkout is built with, as DOCKYARD_VERSION
PROJECT = "dockyard-local-port-test"

pytestmark = pytest.mark.skipif(
    not (
        os.environ.get("DOCKYARD_LOCAL_PORT_TESTS") == "1"
        and platform.system() == "Linux"
        and os.geteuid() == 0
    ),
    reason="changes the host's interfaces: needs root on Linux and DOCKYARD_LOCAL_PORT_TESTS=1",
)


def run(*command: str) -> str:
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def answers(address: str) -> bool:
    """Whether an HTTPS request to the local port at `address` gets any answer at all."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # the self-signed certificate
    connection = http.client.HTTPSConnection(address, PORT, timeout=5, context=context)
    try:
        connection.request("GET", "/")
        connection.getresponse()
    except OSError:
        return False
    finally:
        connection.close()
    return True


@pytest.fixture(scope="module")
def host_addresses() -> Iterator[None]:
    subprocess.run(["ip", "link", "del", INTERFACE], capture_output=True)
    run("ip", "link", "add", INTERFACE, "type", "dummy")
    try:
        run("ip", "addr", "add", f"{NAMED}/24", "dev", INTERFACE)
        run("ip", "addr", "add", f"{OTHER}/24", "dev", INTERFACE)
        run("ip", "-6", "addr", "add", f"{IPV6}/64", "dev", INTERFACE, "nodad")
        run("ip", "link", "set", INTERFACE, "up")
        yield
    finally:
        run("ip", "link", "del", INTERFACE)


@pytest.fixture(scope="module")
def dockyard(host_addresses: None, tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Dockyard with Local sign-in only, installed from .env.example as the README says."""
    run("docker", "build", "--tag", f"ghcr.io/giurlanda/dockyard:{VERSION}", str(REPO))
    directory = install(
        tmp_path_factory.mktemp("installation"),
        {
            "COMPOSE_FILE": "compose.yaml:compose.local.yaml",
            "COMPOSE_PROFILES": "",
            "DOCKYARD_VERSION": VERSION,
            "DOCKYARD_SECRET_KEY": Fernet.generate_key().decode(),
            "DOCKYARD_FIRST_ADMINISTRATOR_EMAIL": "admin@example.com",
            "DOCKYARD_LOCAL_ADDRESS": NAMED,
        },
    )
    started = compose(directory, "--project-name", PROJECT, "up", "--detach", "dockyard")
    assert started.returncode == 0, started.stderr
    try:
        _wait_until_answering(directory)
        yield
    finally:
        compose(directory, "--project-name", PROJECT, "down", "--volumes")


def _wait_until_answering(directory: Path) -> None:
    deadline = time.monotonic() + 120
    while not answers(NAMED):
        if time.monotonic() > deadline:
            logs = compose(directory, "--project-name", PROJECT, "logs", "dockyard").stdout
            pytest.fail(f"Dockyard never answered on {NAMED}:{PORT}:\n{logs}")
        time.sleep(1)


def main_address() -> str:
    """The host's own address on its network: the one it reaches the internet from."""
    route = run("ip", "-4", "route", "get", "1.1.1.1").split()
    return route[route.index("src") + 1]


def test_the_local_port_answers_on_the_address_named_in_env(dockyard: None) -> None:
    assert answers(NAMED)


def test_the_local_port_does_not_answer_on_another_host_address(dockyard: None) -> None:
    for address in (OTHER, main_address(), "127.0.0.1"):
        assert not answers(address), address


def test_the_local_port_does_not_answer_on_ipv6(dockyard: None) -> None:
    for address in (IPV6, "::1"):
        assert not answers(address), address
