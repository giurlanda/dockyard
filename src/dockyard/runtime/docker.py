"""Docker adapter. It reaches Docker only through DOCKER_HOST, which points to the socket proxy."""

from __future__ import annotations

import os

import docker
import requests
from docker.errors import DockerException


class RuntimeUnavailable(Exception):
    pass


class DockerRuntime:
    def __init__(self, client: docker.DockerClient) -> None:
        self._client = client

    @classmethod
    def from_env(cls) -> DockerRuntime:
        try:
            return cls(docker.from_env())
        except DockerException as error:
            raise _unavailable(error) from error

    def check_reachable(self) -> None:
        try:
            self._client.ping()
        except (DockerException, requests.RequestException) as error:
            raise _unavailable(error) from error


def _unavailable(error: Exception) -> RuntimeUnavailable:
    host = os.environ.get("DOCKER_HOST", "the default Docker socket")
    return RuntimeUnavailable(
        f"Dockyard cannot start: Docker is unreachable at {host} ({error}). "
        "Dockyard talks to Docker only through the socket proxy: check that the socket proxy "
        "is running and that DOCKER_HOST points to it."
    )
