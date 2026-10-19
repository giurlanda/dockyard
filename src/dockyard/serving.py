"""Serve every listener of an Instance at once, in one process and one event loop."""

from __future__ import annotations

import asyncio
import logging
import re
import signal
import threading
from collections.abc import Generator, Iterable
from contextlib import contextmanager, suppress

import uvicorn

from .web import ACTIVATE, Listener

STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)
_ACTIVATION_TOKEN = re.compile(rf"^{re.escape(ACTIVATE)}[^?]*")


class _HideActivationTokens(logging.Filter):
    """The access log never shows an Activation link's token: whoever reads the log could
    otherwise choose the person's password."""

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn logs each request with the arguments client, method, path, version, status.
        if isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, path, version, status = record.args
            if isinstance(path, str):
                path = _ACTIVATION_TOKEN.sub(f"{ACTIVATE}…", path)
            record.args = (client, method, path, version, status)
        return True


_HIDE_ACTIVATION_TOKENS = _HideActivationTokens()


class _Server(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Generator[None]:
        yield  # Servers stops them all on a signal: each one alone would stop only itself


class Servers:
    def __init__(self, listeners: Iterable[Listener], *, log_level: str | None = None) -> None:
        self._servers = [
            _Server(
                uvicorn.Config(
                    listener.app,
                    host=listener.host,
                    port=listener.port,
                    log_level=log_level,
                    # The client address is the connection's: the local listener judges by it.
                    proxy_headers=False,
                    ssl_certfile=listener.certificate.certfile if listener.certificate else None,
                    ssl_keyfile=listener.certificate.keyfile if listener.certificate else None,
                )
            )
            for listener in listeners
        ]
        # After the configs: each one configures the log afresh. Added once, however many times.
        logging.getLogger("uvicorn.access").addFilter(_HIDE_ACTIVATION_TOKENS)
        self._signals: list[int] = []

    @property
    def started(self) -> bool:
        return all(server.started for server in self._servers)

    def run(self) -> None:
        """Serve until stopped, by stop() or by SIGINT or SIGTERM in the main thread. As with
        uvicorn.run, the signal then takes its usual course: SIGTERM ends the process.
        Raises SystemExit when a listener cannot start."""
        loop_factory = self._servers[0].config.get_loop_factory()
        asyncio.run(self._serve(), loop_factory=loop_factory)
        with suppress(KeyboardInterrupt):
            for sig in reversed(self._signals):
                signal.raise_signal(sig)

    def stop(self) -> None:
        for server in self._servers:
            server.should_exit = True

    def _on_signal(self, sig: int) -> None:
        self._signals.append(sig)
        if sig == signal.SIGINT and any(server.should_exit for server in self._servers):
            for server in self._servers:
                server.force_exit = True  # Ctrl-C again: stop without waiting for anyone
        self.stop()

    async def _serve(self) -> None:
        loop = asyncio.get_running_loop()
        on_signals = threading.current_thread() is threading.main_thread()
        if on_signals:
            for sig in STOP_SIGNALS:
                loop.add_signal_handler(sig, self._on_signal, sig)
        try:
            await asyncio.gather(*(server.serve() for server in self._servers))
        finally:
            if on_signals:
                for sig in STOP_SIGNALS:
                    loop.remove_signal_handler(sig)
