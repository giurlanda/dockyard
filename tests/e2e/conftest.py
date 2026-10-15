"""Seam C harness: a running Dockyard driven by a real browser.

Each test gets a fresh Instance, served over HTTP from a background thread with the in-memory
Runtime. The browser identifies itself with the development identity, which these tests enable
explicitly.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import uvicorn
from cryptography.fernet import Fernet
from playwright.sync_api import Browser, BrowserContext, Page

from dockyard.runtime.fake import FakeRuntime
from dockyard.settings import Settings
from dockyard.web import create_app

ADMINISTRATOR = "first.administrator@dockyard.example.com"
DEV_HEADER = "X-Dockyard-Dev-Email"
PHONE = {"width": 375, "height": 740}


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@pytest.fixture
def dockyard_url(tmp_path: Path) -> Iterator[str]:
    data_dir = tmp_path / "dockyard-data"
    settings = Settings.from_env(
        {
            "DOCKYARD_SECRET_KEY": Fernet.generate_key().decode(),
            "DOCKYARD_FIRST_ADMINISTRATOR_EMAIL": ADMINISTRATOR,
            "DOCKYARD_DEV_IDENTITY": "true",
            "DOCKYARD_DATA_DIR": str(data_dir),
        }
    )
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings, FakeRuntime()), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("Dockyard did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


OpenAs = Callable[..., Page]


@pytest.fixture
def open_as(browser: Browser, dockyard_url: str) -> Iterator[OpenAs]:
    """A fresh browser session for a person the authentication proxy has verified."""
    contexts: list[BrowserContext] = []

    def open_as(email: str | None, *, viewport: dict[str, int] | None = None) -> Page:
        """email=None: someone who reached Dockyard without going through the proxy."""
        context = browser.new_context(
            base_url=dockyard_url,
            extra_http_headers={DEV_HEADER: email} if email else {},
            locale="en-US",
            **({"viewport": viewport} if viewport else {}),  # type: ignore[arg-type]
        )
        contexts.append(context)
        return context.new_page()

    yield open_as
    for context in contexts:
        context.close()


def fits_the_screen(page: Page) -> bool:
    return bool(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
