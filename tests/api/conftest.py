"""Seam A harness: Dockyard's HTTP API in-process, behind a simulated Cloudflare Access."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from dockyard.runtime.fake import FakeRuntime
from dockyard.settings import Settings
from dockyard.web import Instance, ListenerName, create_instance

ADMINISTRATOR = "first.administrator@example.com"
TEAM = "acme"
AUDIENCE = "dockyard-test-audience"
ISSUER = f"https://{TEAM}.cloudflareaccess.com"
# Where cloudflared reaches Dockyard from: a private address on the Docker network.
CLOUDFLARED = "172.18.0.3"


def new_signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class AccessProxy:
    """Plays Cloudflare Access: issues identity tokens and publishes the keys that sign them."""

    kid = "access-test-key"

    def __init__(self) -> None:
        self._key = new_signing_key()
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self._key.public_key(), as_dict=True)
        self._published = jwt.PyJWKSet.from_dict(
            {"keys": [{**jwk, "kid": self.kid, "alg": "RS256", "use": "sig"}]}
        )
        self._reachable = True

    # The Cloudflare Access certs endpoint, as Dockyard sees it.
    def get_signing_key_from_jwt(self, token: str) -> jwt.PyJWK:
        if not self._reachable:
            raise jwt.PyJWKClientConnectionError("Fail to fetch data from the url, err: timed out")
        kid = jwt.get_unverified_header(token).get("kid")
        for key in self._published.keys:
            if key.key_id == kid:
                return key
        raise jwt.PyJWKClientError(f"Unable to find a signing key that matches: {kid}")

    def token(
        self,
        email: str | None,
        *,
        issuer: str = ISSUER,
        audience: str = AUDIENCE,
        expires_in: int = 600,
        key: rsa.RSAPrivateKey | None = None,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": issuer,
            "aud": [audience],
            "iat": now,
            "nbf": now,
            "exp": now + expires_in,
            "sub": "access-user-id",
        }
        if email is not None:
            claims["email"] = email
        return jwt.encode(claims, key or self._key, algorithm="RS256", headers={"kid": self.kid})

    def headers(self, email: str) -> dict[str, str]:
        return {"Cf-Access-Jwt-Assertion": self.token(email)}

    @contextmanager
    def signing_keys_unreachable(self) -> Iterator[None]:
        self._reachable = False
        try:
            yield
        finally:
            self._reachable = True


def instance_env(data_dir: Path, secret_key: str | None = None) -> dict[str, str]:
    return {
        "DOCKYARD_SECRET_KEY": secret_key or Fernet.generate_key().decode(),
        "DOCKYARD_FIRST_ADMINISTRATOR_EMAIL": ADMINISTRATOR,
        "DOCKYARD_CF_ACCESS_TEAM": TEAM,
        "DOCKYARD_CF_ACCESS_AUD": AUDIENCE,
        "DOCKYARD_DATA_DIR": str(data_dir),
    }


@pytest.fixture(scope="session")
def proxy() -> AccessProxy:
    return AccessProxy()


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime()


class FakeClock:
    """The time the Instance sees, moved on by the test instead of by waiting."""

    def __init__(self) -> None:
        self._now = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta: float) -> None:
        self._now += timedelta(**delta)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def start_instance(
    tmp_path: Path, proxy: AccessProxy, runtime: FakeRuntime, clock: FakeClock
) -> Callable[..., Instance]:
    """Build the whole Instance, every listener of it, with the standard test Instance
    configuration plus overrides.

    An override of None removes that setting. Every start shares the same data, Runtime, clock
    and secret key: it is the same Instance.
    """
    secret_key = Fernet.generate_key().decode()

    def start(**overrides: str | None) -> Instance:
        env = instance_env(tmp_path, secret_key)
        for name, value in overrides.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return create_instance(Settings.from_env(env), runtime, signing_keys=proxy, clock=clock)

    return start


def listener_client(
    instance: Instance, listener: ListenerName, *, address: str = CLOUDFLARED
) -> TestClient:
    """A client of one of the Instance's listeners, reaching it from `address`."""
    return TestClient(instance.listeners[listener].app, client=(address, 50000))


@pytest.fixture
def start_dockyard(start_instance: Callable[..., Instance]) -> Callable[..., TestClient]:
    """Start Dockyard as reached through the tunnel (see start_instance for the overrides)."""

    def start(**overrides: str | None) -> TestClient:
        return listener_client(start_instance(**overrides), ListenerName.TUNNEL)

    return start


@pytest.fixture
def dockyard(start_dockyard: Callable[..., TestClient]) -> TestClient:
    return start_dockyard()


def wait_until(condition: Callable[[], bool], timeout: float = 10) -> bool:
    """For what Dockyard does in the background, such as a clone."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


@pytest.fixture
def add_user(dockyard: TestClient, proxy: AccessProxy) -> Callable[[str], None]:
    """The first Administrator invites the person, who then reaches Dockyard for the first time."""

    def add(email: str) -> None:
        dockyard.post(
            "/api/invitations", json={"email": email}, headers=proxy.headers(ADMINISTRATOR)
        )
        assert dockyard.get("/api/me", headers=proxy.headers(email)).status_code == 200

    return add


def connect_claude_account(
    dockyard: TestClient, headers: dict[str, str], *, start: bool = True
) -> None:
    """Sign in through the login terminal, as a User does (start=False: one already started)."""
    if start:
        dockyard.post("/api/claude-account/login", headers=headers)
    with dockyard.websocket_connect("/ws/claude-account/login", headers=headers) as terminal:
        shown = ""
        while "oauth/authorize" not in shown:
            shown += terminal.receive_bytes().decode(errors="replace")
        terminal.send_json({"type": "input", "data": "the-code-from-claude-ai\r"})
        while "Login successful." not in shown:
            shown += terminal.receive_bytes().decode(errors="replace")
    status = dockyard.get("/api/claude-account", headers=headers).json()
    assert status["connected"], status
