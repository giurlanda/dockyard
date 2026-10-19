"""Start Dockyard: check the Instance configuration and Docker, then serve."""

from __future__ import annotations

import logging
import os
import sys

from .runtime import RuntimeUnavailable
from .runtime.docker import DockerRuntime
from .serving import Servers
from .settings import CloudflareAccess, ConfigError, Settings
from .tls import Certificate, CertificateError, local_certificate
from .web import create_instance

log = logging.getLogger("dockyard")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings.from_env(os.environ)
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 2
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


if __name__ == "__main__":
    sys.exit(main())
