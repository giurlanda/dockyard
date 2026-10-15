"""Seam A harness: Dockyard's HTTP API in-process, behind a simulated Cloudflare Access."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from dockyard.runtime.fake import FakeRuntime
from dockyard.settings import Settings
from dockyard.web import create_app

ADMINISTRATOR = "first.administrator@example.com"
TEAM = "acme"
AUDIENCE = "dockyard-test-audience"
ISSUER = f"https://{TEAM}.cloudflareaccess.com"


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


def instance_env(data_dir: Path) -> dict[str, str]:
    return {
        "DOCKYARD_SECRET_KEY": Fernet.generate_key().decode(),
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


@pytest.fixture
def start_dockyard(
    tmp_path: Path, proxy: AccessProxy, runtime: FakeRuntime
) -> Callable[..., TestClient]:
    """Start Dockyard with the standard test Instance configuration, plus overrides.

    An override of None removes that setting. Every start shares the same data and Runtime.
    """

    def start(**overrides: str | None) -> TestClient:
        env = instance_env(tmp_path)
        for name, value in overrides.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return TestClient(create_app(Settings.from_env(env), runtime, signing_keys=proxy))

    return start


@pytest.fixture
def dockyard(start_dockyard: Callable[..., TestClient]) -> TestClient:
    return start_dockyard()


@pytest.fixture
def add_user(start_dockyard: Callable[..., TestClient]) -> Callable[[str], None]:
    """Until Invitations exist, a User is added the one way the public interface allows: by
    naming them first Administrator for a restart. Every start shares the same data and Runtime.
    """

    def add(email: str) -> None:
        start_dockyard(DOCKYARD_FIRST_ADMINISTRATOR_EMAIL=email)

    return add
