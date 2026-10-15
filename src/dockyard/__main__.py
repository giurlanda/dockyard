"""Start Dockyard: check the Instance configuration and Docker, then serve."""

from __future__ import annotations

import logging
import os
import sys

import uvicorn

from .runtime import RuntimeUnavailable
from .runtime.docker import DockerRuntime
from .settings import ConfigError, Settings
from .web import create_app


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings.from_env(os.environ)
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 2
    try:
        runtime = DockerRuntime.from_env(base_image=settings.base_image, dns=settings.projects_dns)
        runtime.check_reachable()
        runtime.ensure_infrastructure()
    except RuntimeUnavailable as error:
        print(error, file=sys.stderr)
        return 1
    uvicorn.run(create_app(settings, runtime), host=settings.listen_host, port=settings.listen_port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
