"""Instance configuration, read from the environment. Dockyard refuses to start without it."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet

from .runtime import DEFAULT_DNS

TRUE = ("1", "true", "yes", "on")
FALSE = ("", "0", "false", "no", "off")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+$")
TEAM_NAME = re.compile(r"^[A-Za-z0-9-]+$")
GENERATE_SECRET_KEY = "Generate one with: openssl rand -base64 32 | tr '+/' '-_'"
DEFAULT_BASE_IMAGE = "dockyard-base:latest"  # built by compose.yaml from images/base


class ConfigError(Exception):
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(
            "Dockyard cannot start: fix the Instance configuration.\n"
            + "\n".join(f"  - {problem}" for problem in problems)
        )


@dataclass(frozen=True)
class CloudflareAccess:
    """Identity comes from the token Cloudflare Access adds to every request."""

    team: str
    audience: str

    @property
    def issuer(self) -> str:
        return f"https://{self.team}.cloudflareaccess.com"


@dataclass(frozen=True)
class DevelopmentHeader:
    """Identity comes from a plain request header. Only for development, never behind a proxy."""


IdentitySource = CloudflareAccess | DevelopmentHeader


@dataclass(frozen=True)
class Settings:
    secret_key: str
    first_administrator_email: str
    identity: IdentitySource
    data_dir: Path
    listen_host: str
    listen_port: int
    base_image: str
    projects_dns: tuple[str, ...]

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        problems: list[str] = []

        def setting(name: str, purpose: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                problems.append(f"{name} is missing: {purpose}")
            return value

        secret_key = setting(
            "DOCKYARD_SECRET_KEY",
            f"the key that encrypts Git Credentials at rest. {GENERATE_SECRET_KEY}",
        )
        if secret_key and not _is_fernet_key(secret_key):
            problems.append(
                "DOCKYARD_SECRET_KEY is not a valid key: it must be 32 random bytes in url-safe "
                f"base64. {GENERATE_SECRET_KEY}"
            )

        administrator = setting(
            "DOCKYARD_FIRST_ADMINISTRATOR_EMAIL", "the email of the first Administrator."
        )
        if administrator and not EMAIL.match(administrator):
            problems.append(
                f"DOCKYARD_FIRST_ADMINISTRATOR_EMAIL is not an email address: {administrator!r}"
            )

        dev_identity = env.get("DOCKYARD_DEV_IDENTITY", "").strip().lower()
        identity: IdentitySource
        if dev_identity in TRUE:
            identity = DevelopmentHeader()
        else:
            if dev_identity not in FALSE:
                problems.append("DOCKYARD_DEV_IDENTITY must be true or false.")
            team = setting(
                "DOCKYARD_CF_ACCESS_TEAM",
                "your Cloudflare Zero Trust team name (the 'acme' in acme.cloudflareaccess.com).",
            )
            if team and not TEAM_NAME.match(team):
                problems.append(
                    "DOCKYARD_CF_ACCESS_TEAM must be the team name only, "
                    f"e.g. 'acme' for acme.cloudflareaccess.com, not {team!r}."
                )
            audience = setting(
                "DOCKYARD_CF_ACCESS_AUD",
                "the Application Audience (AUD) tag of the Cloudflare Access application "
                "in front of Dockyard.",
            )
            identity = CloudflareAccess(team=team.lower(), audience=audience)

        listen_port = env.get("DOCKYARD_LISTEN_PORT", "").strip() or "8000"
        if not (listen_port.isdigit() and 1 <= int(listen_port) <= 65535):
            problems.append(f"DOCKYARD_LISTEN_PORT must be a port number, not {listen_port!r}.")

        dns = env.get("DOCKYARD_PROJECTS_DNS", "").strip()
        projects_dns = tuple(server.strip() for server in dns.split(",")) if dns else DEFAULT_DNS
        if not all(_is_ip_address(server) for server in projects_dns):
            problems.append(
                "DOCKYARD_PROJECTS_DNS must be IP addresses separated by commas, "
                f"e.g. 1.1.1.1,9.9.9.9, not {dns!r}."
            )

        if problems:
            raise ConfigError(problems)
        return cls(
            secret_key=secret_key,
            first_administrator_email=administrator,
            identity=identity,
            data_dir=Path(env.get("DOCKYARD_DATA_DIR", "").strip() or "/data"),
            listen_host=env.get("DOCKYARD_LISTEN_HOST", "").strip() or "127.0.0.1",
            listen_port=int(listen_port),
            base_image=env.get("DOCKYARD_BASE_IMAGE", "").strip() or DEFAULT_BASE_IMAGE,
            projects_dns=projects_dns,
        )


def _is_ip_address(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def _is_fernet_key(key: str) -> bool:
    try:
        Fernet(key)
    except ValueError:
        return False
    return True
