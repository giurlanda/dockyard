"""The Base Image: what every Project environment and every Image starts from."""

import docker
import pytest

pytestmark = pytest.mark.slow


@pytest.fixture
def run(docker_daemon: docker.DockerClient, base_image: str) -> "Run":
    return Run(docker_daemon, base_image)


class Run:
    def __init__(self, daemon: docker.DockerClient, image: str) -> None:
        self._daemon = daemon
        self._image = image

    def __call__(self, script: str) -> str:
        container = self._daemon.containers.create(
            self._image, ["bash", "-lc", script], network_disabled=True
        )
        try:
            container.start()
            status = container.wait(timeout=60)["StatusCode"]
            output: str = container.logs().decode().strip()
        finally:
            container.remove(force=True)
        assert status == 0, output
        return output


def test_it_runs_as_a_non_root_user(run: Run) -> None:
    assert run("id -u") == "1000"
    assert run("id -un") == "ubuntu"


@pytest.mark.parametrize("tool", ["claude", "git", "gh", "glab", "node", "npm", "python3", "pip3"])
def test_it_has_the_tools_projects_need(run: Run, tool: str) -> None:
    assert run(f"{tool} --version >/dev/null && echo works") == "works"


def test_claude_code_is_installed_outside_the_home(run: Run) -> None:
    """The home is a persistent volume: a binary inside it would never be updated."""
    location = run('readlink -f "$(command -v claude)"')

    assert not location.startswith("/home/")
    assert run("ls -A ~/.local 2>/dev/null | wc -l") == "0"


def test_claude_code_does_not_update_itself(run: Run) -> None:
    assert run("echo $DISABLE_AUTOUPDATER") == "1"


def test_claude_code_reads_its_config_from_the_users_claude_config(run: Run) -> None:
    assert run("echo $CLAUDE_CONFIG_DIR") == "/home/ubuntu/.claude"


def test_run_dockyard_belongs_to_the_non_root_user(run: Run) -> None:
    assert run("stat -c %U /run/dockyard") == "ubuntu"
