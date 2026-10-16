"""The host firewall: Projects reach the internet but not the LAN or the host.

These tests change the host's firewall, network namespaces and systemd units, so they run only as
root on a Linux host with Docker, and only when DOCKYARD_FIREWALL_TESTS=1 (CI sets it). A network
namespace stands in for the LAN: it has an address in every private range, and a web server
answers on all of them.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import docker
import pytest

from dockyard.runtime import DEFAULT_DNS
from dockyard.runtime.docker import PROJECTS_BRIDGE, PROJECTS_NETWORK, DockerRuntime

REPO = Path(__file__).parents[2]
SCRIPT = REPO / "host" / "dockyard-firewall.sh"
UNIT = REPO / "host" / "dockyard-firewall.service"
INSTALLED_SCRIPT = Path("/usr/local/sbin/dockyard-firewall")
INSTALLED_UNIT = Path("/etc/systemd/system/dockyard-firewall.service")
TEST_DROP_IN = Path("/etc/systemd/system/dockyard-firewall.service.d/test.conf")
PROBE_IMAGE = "curlimages/curl:8.11.1"
CHAIN = "DOCKYARD-PROJECTS"  # the script's own chain
LAN = "dockyard-lan"  # the network namespace standing in for the LAN
LAN_PORT = 8765
HOST_PORT = 8766
# One LAN address per private range, on the namespace side of a veth pair; the host side has .1.
LAN_HOSTS = {
    "10.0.0.0/8": "10.250.250.2",
    "172.16.0.0/12": "172.29.250.2",
    "192.168.0.0/16": "192.168.250.2",
    "169.254.0.0/16": "169.254.250.2",
    "100.64.0.0/10": "100.64.250.2",
}
SECOND_LAN_HOST = "192.168.250.3"

pytestmark = pytest.mark.skipif(
    not (
        os.environ.get("DOCKYARD_FIREWALL_TESTS") == "1"
        and platform.system() == "Linux"
        and os.geteuid() == 0
    ),
    reason="changes the host firewall: needs root on Linux and DOCKYARD_FIREWALL_TESTS=1",
)


def run(*command: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(command, check=True, capture_output=True, text=True, env=env).stdout


def apply_firewall(allowed_ips: str = "") -> None:
    run("bash", str(SCRIPT), env={**os.environ, "DOCKYARD_ALLOWED_IPS": allowed_ips})


def remove_firewall() -> None:
    """Take out every rule the script adds, as on a host where it never ran."""
    for chain in ("DOCKER-USER", "INPUT"):
        for rule in run("iptables", "-S", chain).splitlines():
            if PROJECTS_BRIDGE in rule:
                run("iptables", *rule.replace("-A", "-D", 1).split())
    subprocess.run(["iptables", "-F", CHAIN], capture_output=True)
    subprocess.run(["iptables", "-X", CHAIN], capture_output=True)


def firewall_rules() -> list[str]:
    rules = run("iptables", "-S").splitlines()
    return [rule for rule in rules if PROJECTS_BRIDGE in rule or CHAIN in rule]


@pytest.fixture(scope="module")
def client() -> docker.DockerClient:
    client = docker.from_env()
    client.images.pull(PROBE_IMAGE)
    return client


@pytest.fixture(scope="module")
def projects_network(client: docker.DockerClient) -> str:
    """The Projects network, created by Dockyard as at startup."""
    DockerRuntime(client, base_image="unused").ensure_infrastructure()
    return PROJECTS_NETWORK


@pytest.fixture(scope="module")
def lan() -> Iterator[None]:
    subprocess.run(["ip", "netns", "del", LAN], capture_output=True)
    run("ip", "netns", "add", LAN)
    run("ip", "link", "add", "dy-lan", "type", "veth", "peer", "name", "dy-lan-ns")
    run("ip", "link", "set", "dy-lan-ns", "netns", LAN)
    for address in [*LAN_HOSTS.values(), SECOND_LAN_HOST]:
        gateway = address.rsplit(".", 1)[0] + ".1"
        subprocess.run(["ip", "addr", "add", f"{gateway}/24", "dev", "dy-lan"], capture_output=True)
        run("ip", "netns", "exec", LAN, "ip", "addr", "add", f"{address}/24", "dev", "dy-lan-ns")
    run("ip", "link", "set", "dy-lan", "up")
    run("ip", "netns", "exec", LAN, "ip", "link", "set", "dy-lan-ns", "up")
    run("ip", "netns", "exec", LAN, "ip", "link", "set", "lo", "up")
    server = subprocess.Popen(
        ["ip", "netns", "exec", LAN, "python3", "-m", "http.server", str(LAN_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield
    finally:
        server.terminate()
        server.wait()
        subprocess.run(["ip", "link", "del", "dy-lan"], capture_output=True)
        run("ip", "netns", "del", LAN)


@pytest.fixture(scope="module")
def host_server() -> Iterator[None]:
    """A web server on every address of the host."""
    server = subprocess.Popen(
        ["python3", "-m", "http.server", str(HOST_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield
    finally:
        server.terminate()
        server.wait()


@pytest.fixture(scope="module")
def host_addresses(projects_network: str, client: docker.DockerClient) -> list[str]:
    """The host as a Project sees it: its gateway on the Projects bridge and its main address."""
    config = client.networks.get(projects_network).attrs["IPAM"]["Config"]
    gateway = config[0]["Gateway"]
    route = run("ip", "-4", "route", "get", "1.1.1.1").split()
    return [gateway, route[route.index("src") + 1]]


@pytest.fixture(autouse=True)
def clean_firewall() -> Iterator[None]:
    remove_firewall()
    yield
    remove_firewall()


Reachable = Callable[[str], bool]


@pytest.fixture
def reachable(
    client: docker.DockerClient, projects_network: str, lan: None, host_server: None
) -> Reachable:
    def reachable(url: str) -> bool:
        """Whether a Project reaches the URL: a container on the Projects network with its DNS."""
        container = client.containers.run(
            PROBE_IMAGE,
            ["-sS", "-o", "/dev/null", "--max-time", "5", url],
            network=projects_network,
            dns=list(DEFAULT_DNS),
            detach=True,
        )
        try:
            return container.wait()["StatusCode"] == 0
        finally:
            container.remove(force=True)

    return reachable


def lan_url(address: str) -> str:
    return f"http://{address}:{LAN_PORT}/"


def host_url(address: str) -> str:
    return f"http://{address}:{HOST_PORT}/"


def test_without_the_firewall_projects_reach_the_lan_and_the_host(
    reachable: Reachable, host_addresses: list[str]
) -> None:
    # The control: what the firewall must take away is reachable without it.
    for address in LAN_HOSTS.values():
        assert reachable(lan_url(address)), address
    for address in host_addresses:
        assert reachable(host_url(address)), address


@pytest.mark.parametrize("site", ["https://github.com", "https://gitlab.com"])
def test_projects_reach_github_and_gitlab_by_name(reachable: Reachable, site: str) -> None:
    apply_firewall()

    assert reachable(site)


@pytest.mark.parametrize("private_range", LAN_HOSTS)
def test_projects_cannot_reach_private_ranges(reachable: Reachable, private_range: str) -> None:
    apply_firewall()

    assert not reachable(lan_url(LAN_HOSTS[private_range]))


def test_projects_cannot_reach_the_host(reachable: Reachable, host_addresses: list[str]) -> None:
    apply_firewall()

    for address in host_addresses:
        assert not reachable(host_url(address)), address


def test_allowed_ips_are_reachable_and_nothing_else(reachable: Reachable) -> None:
    apply_firewall(allowed_ips=f"{LAN_HOSTS['192.168.0.0/16']}, 10.250.250.0/24")

    assert reachable(lan_url(LAN_HOSTS["192.168.0.0/16"]))
    assert reachable(lan_url(LAN_HOSTS["10.0.0.0/8"]))
    assert not reachable(lan_url(SECOND_LAN_HOST))
    assert not reachable(lan_url(LAN_HOSTS["172.16.0.0/12"]))


def test_an_ip_no_longer_allowed_is_blocked_again(reachable: Reachable) -> None:
    apply_firewall(allowed_ips=LAN_HOSTS["192.168.0.0/16"])
    apply_firewall(allowed_ips="")

    assert not reachable(lan_url(LAN_HOSTS["192.168.0.0/16"]))


def test_running_the_script_again_does_not_duplicate_rules() -> None:
    apply_firewall(allowed_ips=SECOND_LAN_HOST)
    rules = firewall_rules()

    apply_firewall(allowed_ips=SECOND_LAN_HOST)

    assert firewall_rules() == rules


def test_the_script_refuses_an_allowed_ip_that_is_not_an_address() -> None:
    with pytest.raises(subprocess.CalledProcessError) as refused:
        apply_firewall(allowed_ips="192.168.1.50; reboot")

    assert "DOCKYARD_ALLOWED_IPS" in refused.value.stderr


@pytest.fixture
def installed_service() -> Iterator[None]:
    if shutil.which("systemctl") is None or not Path("/run/systemd/system").exists():
        pytest.skip("needs systemd")
    if INSTALLED_UNIT.exists():
        pytest.skip("the firewall service is installed on this host: left as it is")
    shutil.copy(SCRIPT, INSTALLED_SCRIPT)
    INSTALLED_SCRIPT.chmod(0o755)
    shutil.copy(UNIT, INSTALLED_UNIT)
    TEST_DROP_IN.parent.mkdir(exist_ok=True)
    TEST_DROP_IN.write_text(f"[Service]\nEnvironment=DOCKYARD_ALLOWED_IPS={SECOND_LAN_HOST}\n")
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "dockyard-firewall")
    try:
        yield
    finally:
        run("systemctl", "disable", "--now", "dockyard-firewall")
        INSTALLED_UNIT.unlink()
        shutil.rmtree(TEST_DROP_IN.parent)
        INSTALLED_SCRIPT.unlink()
        run("systemctl", "daemon-reload")


def test_the_service_applies_the_rules_again_after_docker_starts(
    installed_service: None, reachable: Reachable
) -> None:
    remove_firewall()

    run("systemctl", "restart", "docker")

    assert not reachable(lan_url(LAN_HOSTS["192.168.0.0/16"]))
    assert reachable(lan_url(SECOND_LAN_HOST))
