# Local sign-in only on its own listener, never through the tunnel

Local sign-in (email, password and optional Second factor) is accepted only on a dedicated HTTPS port that the host publishes on the addresses the installer names, and only from clients in the Local networks. The port the Cloudflare Tunnel reaches never accepts a password, and the dedicated port never trusts a Cloudflare Access token. Even inside the Local networks, Dockyard refuses clients whose address belongs to Docker itself (the bridge gateway and the Docker networks).

Checking the client address alone is not enough: every request that comes through the tunnel reaches Dockyard from `cloudflared`, a private Docker address that a "private networks only" rule would let in, so internet traffic would pass as LAN. The same happens when Docker publishes a port on IPv6 and relays it through `docker-proxy`: the connection arrives from the bridge gateway. Keeping the two ways in on separate listeners makes the channel, not the address, decide which proof of identity counts; the address check and the explicit IPv4 binding are then defence in depth against a port published where it should not be.

## Considered Options

- **One listener, client address allowlist only**: rejected because tunnel and `docker-proxy` traffic arrive from private addresses.
- **Published port only, no address check in Dockyard**: rejected because ports Docker publishes bypass host firewalls such as UFW, and a host with a public address would expose the sign-in page to the internet.
- **Plain HTTP behind a reverse proxy the installer provides**: rejected because the address check would have to trust the proxy's forwarded headers, and the transport security of passwords would depend on configuration outside Dockyard.
