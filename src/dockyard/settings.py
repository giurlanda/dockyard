"""Instance configuration, read from the environment. Dockyard refuses to start without it."""

from __future__ import annotations

import ipaddress
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

from .projects import DEFAULT_LIMITS, GIB, MIN_CPUS, MIN_MEMORY, Limits
from .runtime import DEFAULT_DNS

TRUE = ("1", "true", "yes", "on")
FALSE = ("", "0", "false", "no", "off")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+$")
TEAM_NAME = re.compile(r"^[A-Za-z0-9-]+$")
GENERATE_SECRET_KEY = "Generate one with: openssl rand -base64 32 | tr '+/' '-_'"
# Published by the Release workflow. compose.yaml always sets DOCKYARD_BASE_IMAGE, to the Base
# Image of DOCKYARD_VERSION: this default serves Dockyard run outside compose.
DEFAULT_BASE_IMAGE = "ghcr.io/giurlanda/dockyard-base:latest"
# The private ranges, and the range VPNs such as Tailscale give their devices.
DEFAULT_LOCAL_NETWORKS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10"
EVERY_INTERFACE = ipaddress.IPv4Address("0.0.0.0")


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
class LocalSignIn:
    """Local sign-in: Dockyard's own sign-in page, on a listener of its own (ADR 0004)."""

    port: int  # inside the container
    address: ipaddress.IPv4Address  # of the host, where the port is published
    networks: tuple[ipaddress.IPv4Network, ...]  # the Local networks
    url: str | None  # where people open it, when not at the address
    published_port: int  # on the host, at the address

    @property
    def on_every_interface(self) -> bool:
        return self.address == EVERY_INTERFACE

    @property
    def base_url(self) -> str:
        """Where people open Local sign-in: what Activation links point to."""
        return (self.url or f"https://{self.address}:{self.published_port}").rstrip("/")


@dataclass(frozen=True)
class Settings:
    secret_key: str
    first_administrator_email: str
    identity: IdentitySource | None  # of the tunnel listener; None: there is no tunnel listener
    local: LocalSignIn | None
    data_dir: Path
    listen_host: str
    listen_port: int
    base_image: str
    projects_dns: tuple[str, ...]
    limits: Limits

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
        identity: IdentitySource | None
        if dev_identity in TRUE:
            identity = DevelopmentHeader()
        else:
            if dev_identity not in FALSE:
                problems.append("DOCKYARD_DEV_IDENTITY must be true or false.")
            identity = _cloudflare_access(env, problems)

        listen_port = env.get("DOCKYARD_LISTEN_PORT", "").strip() or "8000"
        if not _is_port(listen_port):
            problems.append(f"DOCKYARD_LISTEN_PORT must be a port number, not {listen_port!r}.")

        local = _local_sign_in(env, problems)
        if local and identity and _is_port(listen_port) and local.port == int(listen_port):
            problems.append(
                f"DOCKYARD_LOCAL_PORT must differ from DOCKYARD_LISTEN_PORT ({listen_port}): "
                "each way in has a listener of its own."
            )
        if identity is None and local is None:
            problems.append(
                "No way in is configured, so nobody could reach Dockyard. Enable Cloudflare "
                "Access (DOCKYARD_CF_ACCESS_TEAM and DOCKYARD_CF_ACCESS_AUD), Local sign-in "
                "(DOCKYARD_LOCAL_PORT, set by compose.local.yaml, and DOCKYARD_LOCAL_ADDRESS), "
                "or both."
            )

        dns = env.get("DOCKYARD_PROJECTS_DNS", "").strip()
        projects_dns = tuple(server.strip() for server in dns.split(",")) if dns else DEFAULT_DNS
        if not all(_is_ip_address(server) for server in projects_dns):
            problems.append(
                "DOCKYARD_PROJECTS_DNS must be IP addresses separated by commas, "
                f"e.g. 1.1.1.1,9.9.9.9, not {dns!r}."
            )

        def number(name: str, default: float, least: float, unit: str) -> float:
            text = env.get(name, "").strip()
            if not text:
                return default
            try:
                value = float(text)
            except ValueError:
                value = math.nan
            if not (math.isfinite(value) and value >= least):
                problems.append(
                    f"{name} must be a number of {unit}, at least {least:g}, not {text!r}."
                )
            return value

        max_cpus = number("DOCKYARD_PROJECT_MAX_CPUS", DEFAULT_LIMITS.max_cpus, MIN_CPUS, "CPUs")
        max_memory = number(
            "DOCKYARD_PROJECT_MAX_MEMORY_GIB",
            DEFAULT_LIMITS.max_memory / GIB,
            MIN_MEMORY / GIB,
            "GiB",
        )
        max_active = number(
            "DOCKYARD_MAX_ACTIVE_PROJECTS", DEFAULT_LIMITS.max_active, 1, "Projects per User"
        )
        if math.isfinite(max_active) and not max_active.is_integer():
            problems.append(
                f"DOCKYARD_MAX_ACTIVE_PROJECTS must be a whole number, not {max_active!r}."
            )

        if problems:
            raise ConfigError(problems)
        return cls(
            secret_key=secret_key,
            first_administrator_email=administrator,
            identity=identity,
            local=local,
            data_dir=Path(env.get("DOCKYARD_DATA_DIR", "").strip() or "/data"),
            listen_host=env.get("DOCKYARD_LISTEN_HOST", "").strip() or "127.0.0.1",
            listen_port=int(listen_port),
            base_image=env.get("DOCKYARD_BASE_IMAGE", "").strip() or DEFAULT_BASE_IMAGE,
            projects_dns=projects_dns,
            limits=Limits(
                max_cpus=max_cpus, max_memory=round(max_memory * GIB), max_active=int(max_active)
            ),
        )


def _cloudflare_access(env: Mapping[str, str], problems: list[str]) -> CloudflareAccess | None:
    """Both settings or neither: one alone is a typo that would turn Cloudflare off."""
    team = env.get("DOCKYARD_CF_ACCESS_TEAM", "").strip()
    audience = env.get("DOCKYARD_CF_ACCESS_AUD", "").strip()
    if not team and not audience:
        return None
    if not team:
        problems.append(
            "DOCKYARD_CF_ACCESS_TEAM is missing: your Cloudflare Zero Trust team name (the 'acme' "
            "in acme.cloudflareaccess.com). Set both Cloudflare Access settings, or neither."
        )
    elif not TEAM_NAME.match(team):
        problems.append(
            "DOCKYARD_CF_ACCESS_TEAM must be the team name only, "
            f"e.g. 'acme' for acme.cloudflareaccess.com, not {team!r}."
        )
    if not audience:
        problems.append(
            "DOCKYARD_CF_ACCESS_AUD is missing: the Application Audience (AUD) tag of the "
            "Cloudflare Access application in front of Dockyard. Set both Cloudflare Access "
            "settings, or neither."
        )
    return CloudflareAccess(team=team.lower(), audience=audience)


def _local_sign_in(env: Mapping[str, str], problems: list[str]) -> LocalSignIn | None:
    """On when the local port is set; the other local settings matter only then."""
    port = env.get("DOCKYARD_LOCAL_PORT", "").strip()
    if not port:
        return None
    if not _is_port(port):
        problems.append(f"DOCKYARD_LOCAL_PORT must be a port number, not {port!r}.")

    text = env.get("DOCKYARD_LOCAL_ADDRESS", "").strip()
    address = EVERY_INTERFACE
    try:
        address = ipaddress.IPv4Address(text)
    except ValueError:
        if text:
            problems.append(
                "DOCKYARD_LOCAL_ADDRESS must be an IPv4 address of the host, e.g. 192.168.1.10, "
                f"not {text!r}: Local sign-in is never published on IPv6."
            )
        else:
            problems.append(
                "DOCKYARD_LOCAL_ADDRESS is missing: the host's IPv4 address on the LAN or VPN "
                "where Local sign-in is published, e.g. 192.168.1.10."
            )

    listed = env.get("DOCKYARD_LOCAL_NETWORKS", "").strip() or DEFAULT_LOCAL_NETWORKS
    networks: list[ipaddress.IPv4Network] = []
    try:
        networks = [ipaddress.IPv4Network(cidr.strip(), strict=False) for cidr in listed.split(",")]
    except ValueError:
        problems.append(
            "DOCKYARD_LOCAL_NETWORKS must be IPv4 ranges separated by commas, e.g. "
            f"192.168.1.0/24,100.64.0.0/10, not {listed!r}."
        )

    url = env.get("DOCKYARD_LOCAL_URL", "").strip() or None
    if url is not None:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            problems.append(
                "DOCKYARD_LOCAL_URL must be the https:// address where people open Local "
                f"sign-in, e.g. https://dockyard.lan:8443, not {url!r}."
            )

    # Set by compose.local.yaml; the port inside the container when nothing relays it.
    published_port = env.get("DOCKYARD_LOCAL_HOST_PORT", "").strip() or port
    if not _is_port(published_port):
        problems.append(f"DOCKYARD_LOCAL_HOST_PORT must be a port number, not {published_port!r}.")

    return LocalSignIn(
        port=int(port) if _is_port(port) else 0,
        address=address,
        networks=tuple(networks),
        url=url,
        published_port=int(published_port) if _is_port(published_port) else 0,
    )


def _is_port(text: str) -> bool:
    return text.isdigit() and 1 <= int(text) <= 65535


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
