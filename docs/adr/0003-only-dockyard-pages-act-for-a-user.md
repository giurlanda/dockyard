# Only Dockyard's own pages act for a User

Every WebSocket, and every request that changes something (POST, PUT, PATCH, DELETE), is refused when the browser says it comes from another site: its `Origin` header must name the same host as the request's `Host`. The authentication proxy's cookie proves who the person is, but a browser also attaches it to requests that another site makes it send. Without this check, any page a User visits could open their login terminal, and later their Projects' Terminals, and type into them. Requests without `Origin` (not sent by a browser) are judged by identity alone.

This adds a deployment requirement: the proxy in front of Dockyard must pass the original `Host` header through. Cloudflare Tunnel does so by default.

## Considered Options

- **Rely on the proxy cookie's SameSite attribute**: rejected because it is set by the proxy's configuration, not by Dockyard, and WebSockets are where the damage would be.
- **CSRF tokens in forms and WebSocket URLs**: rejected for the MVP; the Origin check covers the same browsers with no state.
