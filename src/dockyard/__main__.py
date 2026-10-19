"""Start Dockyard: check the Instance configuration and Docker, then serve.

With a command, do that instead, on the same data, for whoever has a shell in the container:
`docker compose exec dockyard python -m dockyard <command>`.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from collections.abc import Sequence

from .accounts import Accounts, normalise_email
from .clock import SystemClock
from .database import Database
from .local_sign_in import MIN_PASSWORD_LENGTH, LocalSignIn, NotAUser, PasswordRefused, PasswordRule
from .runtime import RuntimeUnavailable
from .runtime.docker import DockerRuntime
from .serving import Servers
from .settings import CloudflareAccess, ConfigError, Settings
from .tls import Certificate, CertificateError, local_certificate
from .web import create_instance

log = logging.getLogger("dockyard")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dockyard", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", title="commands")
    set_password = commands.add_parser(
        "set-password",
        help="give a User a password for Local sign-in, signing them out everywhere",
    )
    set_password.add_argument("email", help="a User's, or the first Administrator's")
    arguments = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings.from_env(os.environ)
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 2
    if arguments.command == "set-password":
        return _set_password(settings, arguments.email)
    return _serve(settings)


def _serve(settings: Settings) -> int:
    certificate = None
    try:
        if settings.local is not None:
            certificate = local_certificate(settings.data_dir, settings.local)
    except CertificateError as error:
        print(f"Dockyard cannot start: {error}", file=sys.stderr)
        return 2
    _announce_ways_in(settings, certificate)
    try:
        runtime = DockerRuntime.from_env(base_image=settings.base_image, dns=settings.projects_dns)
        runtime.check_reachable()
        runtime.ensure_infrastructure()
        instance = create_instance(settings, runtime, certificate=certificate)
    except RuntimeUnavailable as error:
        print(error, file=sys.stderr)
        return 1
    Servers(instance.listeners.values()).run()
    return 0


def _announce_ways_in(settings: Settings, certificate: Certificate | None) -> None:
    """What the Administrator checks at a glance: which ways in are on, and where."""
    cloudflare = isinstance(settings.identity, CloudflareAccess)
    log.info("Cloudflare Access: %s", "on" if cloudflare else "off")
    local = settings.local
    if local is None:
        log.info("Local sign-in: off")
        return
    log.info("Local sign-in: on, port %d, published on %s", local.port, local.address)
    if certificate is not None:
        log.info(
            "Local sign-in certificate (%s), SHA-256 fingerprint: %s",
            "provided" if certificate.provided else "self-signed",
            certificate.fingerprint,
        )
    if local.on_every_interface:
        log.warning(
            "DOCKYARD_LOCAL_ADDRESS is 0.0.0.0: Local sign-in is published on every interface "
            "of the host, which may reach beyond the LAN. Name the host's LAN or VPN address."
        )


_PASSWORD_RULES = {
    PasswordRule.TOO_SHORT: f"The password must be at least {MIN_PASSWORD_LENGTH} characters long.",
    PasswordRule.EQUALS_EMAIL: "The password must not be the email.",
}


def _set_password(settings: Settings, email: str) -> int:
    if settings.local is None:
        print(
            "Warning: Local sign-in is off (DOCKYARD_LOCAL_PORT is not set): the password will "
            "work once it is on.",
            file=sys.stderr,
        )
    database = Database(settings.data_dir / "dockyard.db")
    database.migrate()
    # As at every start: the first Administrator is a User even before Dockyard first started.
    Accounts(database).bootstrap_first_administrator(settings.first_administrator_email)
    local_sign_in = LocalSignIn(database, SystemClock())

    password = _ask("New password: ")
    if _ask("The same password again: ") != password:
        print("The two passwords differ: nothing was changed.", file=sys.stderr)
        return 1
    try:
        local_sign_in.set_password(email, password)
    except PasswordRefused as refusal:
        print(f"{_PASSWORD_RULES[refusal.rule]} Nothing was changed.", file=sys.stderr)
        return 1
    except NotAUser:
        print(
            f"{email} is neither a User nor the first Administrator of this Instance: nothing "
            "was changed. A person becomes a User through an Invitation.",
            file=sys.stderr,
        )
        return 1
    print(f"Password set for {normalise_email(email)}: every session of theirs is signed out.")
    return 0


def _ask(prompt: str) -> str:
    """Without echo at a terminal; from what is piped in otherwise."""
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    print(prompt, end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().removesuffix("\n")


if __name__ == "__main__":
    sys.exit(main())
