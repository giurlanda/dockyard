"""One Instance, served on each of its listeners: the ways in share the same Users and Projects."""

import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dockyard.runtime.fake import FakeRuntime
from dockyard.serving import Servers
from dockyard.settings import Settings
from dockyard.web import Instance, Listener, ListenerName, create_instance

from .conftest import ADMINISTRATOR, AccessProxy, instance_env, listener_client


def test_the_tunnel_listener_admits_a_user_through_cloudflare_access(
    start_instance: Callable[..., Instance], proxy: AccessProxy
) -> None:
    tunnel = listener_client(start_instance(), ListenerName.TUNNEL)

    response = tunnel.get("/api/me", headers=proxy.headers(ADMINISTRATOR))

    assert response.json() == {"email": ADMINISTRATOR, "role": "administrator"}


def test_without_more_ways_in_the_instance_has_only_the_tunnel_listener_where_it_was(
    start_instance: Callable[..., Instance],
) -> None:
    instance = start_instance(DOCKYARD_LISTEN_HOST=None, DOCKYARD_LISTEN_PORT=None)

    assert [
        (name, listener.host, listener.port) for name, listener in instance.listeners.items()
    ] == [(ListenerName.TUNNEL, "127.0.0.1", 8000)]


def test_an_instance_goes_by_real_time_unless_given_a_clock(
    tmp_path: Path, runtime: FakeRuntime
) -> None:
    instance = create_instance(Settings.from_env(instance_env(tmp_path)), runtime)

    before = datetime.now(UTC)
    seen = instance.clock.now()

    assert before <= seen <= datetime.now(UTC)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def _accepts_connections(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


def test_every_listener_is_served_until_dockyard_stops(
    start_instance: Callable[..., Instance], proxy: AccessProxy
) -> None:
    port = _free_port()
    instance = start_instance(DOCKYARD_LISTEN_HOST="127.0.0.1", DOCKYARD_LISTEN_PORT=str(port))
    servers = Servers(instance.listeners.values(), log_level="warning")
    serving = threading.Thread(target=servers.run, daemon=True)
    serving.start()
    deadline = time.monotonic() + 10
    while not servers.started:
        assert time.monotonic() < deadline and serving.is_alive(), "Dockyard did not start"
        time.sleep(0.05)

    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/me", headers=proxy.headers(ADMINISTRATOR)
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200

    servers.stop()
    serving.join(timeout=10)
    assert not serving.is_alive()
    assert not _accepts_connections(port)


def test_when_a_listener_cannot_start_dockyard_stops_every_listener_and_fails(
    start_instance: Callable[..., Instance],
) -> None:
    port = _free_port()
    instance = start_instance(DOCKYARD_LISTEN_HOST="127.0.0.1", DOCKYARD_LISTEN_PORT=str(port))
    tunnel = instance.listeners[ListenerName.TUNNEL]
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        busy = Listener(tunnel.app, "127.0.0.1", taken.getsockname()[1])
        servers = Servers([tunnel, busy], log_level="critical")

        with pytest.raises(SystemExit) as failure:
            servers.run()

    assert failure.value.code != 0
    assert not _accepts_connections(port)


SERVE_AN_INSTANCE = """
import os, sys
from dockyard.runtime.fake import FakeRuntime
from dockyard.serving import Servers
from dockyard.settings import Settings
from dockyard.web import create_instance

Servers(create_instance(Settings.from_env(os.environ), FakeRuntime()).listeners.values()).run()
sys.exit(0)
"""


def test_on_sigterm_dockyard_stops_every_listener_and_ends_by_the_signal(tmp_path: Path) -> None:
    port = _free_port()
    env = {**instance_env(tmp_path), "DOCKYARD_LISTEN_PORT": str(port)}
    dockyard = subprocess.Popen(
        [sys.executable, "-c", SERVE_AN_INSTANCE],
        env={"PATH": os.environ["PATH"], **env},
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        while not _accepts_connections(port):
            assert time.monotonic() < deadline and dockyard.poll() is None, "Dockyard did not start"
            time.sleep(0.05)

        dockyard.send_signal(signal.SIGTERM)

        assert dockyard.wait(timeout=10) == -signal.SIGTERM
    finally:
        dockyard.kill()
    assert not _accepts_connections(port)
