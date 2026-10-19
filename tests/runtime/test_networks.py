"""Runtime contract: Docker's own networks, which the local listener refuses clients from."""

from dockyard.runtime import Runtime


def test_dockers_networks_are_known_once_the_infrastructure_is_there(runtime: Runtime) -> None:
    runtime.ensure_infrastructure()

    networks = runtime.networks()

    assert networks
    assert all(network.num_addresses > 1 for network in networks)
