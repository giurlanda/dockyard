**English** · [Italiano](README.it.md)

# Dockyard

Dockyard is a self-hosted, multi-user web application that turns a GitHub or GitLab repository
into a Project: an isolated environment where Claude works on the code, always reachable from the
Claude app on your phone or from a browser. Each User connects their own claude.ai subscription;
Projects reach the internet but not your LAN, the host or each other.

Dockyard is an independent open source project. It is not made or endorsed by Anthropic.

## Before you start

You need:

- **A Linux machine with Docker** (Docker Engine with the compose plugin) and
  `systemd`, for the host firewall. Ubuntu 24.04 is the reference. amd64 and arm64 are both
  published.
- **At least one way in**, and you may have both:
  - **Cloudflare**, to reach Dockyard from anywhere: a domain on Cloudflare and a Cloudflare Zero
    Trust account (the free plan is enough). Dockyard publishes no port for it: people come in
    through a Cloudflare Tunnel, after Cloudflare Access has verified who they are.
  - **Local sign-in**, to reach Dockyard from your LAN or VPN without Cloudflare: Dockyard's own
    sign-in page, with email, password and an optional Second factor, over HTTPS on a port the
    host publishes only on the IPv4 address you name. It accepts only the Local networks, never
    the internet and never the tunnel.
- **A claude.ai subscription for every User: Pro, Max, Team or Enterprise.** Projects run Claude
  with Remote Control, which works only with a claude.ai subscription. API keys and long-lived
  tokens (such as one made with `claude setup-token`) disable it. On a Team or Enterprise plan, an
  Owner must first enable Remote Control for the organisation.

## Install

### 1. Get the files

```sh
sudo git clone https://github.com/giurlanda/dockyard.git /opt/dockyard
sudo chown -R "$USER" /opt/dockyard
cd /opt/dockyard
cp .env.example .env
```

Dockyard runs from published images; the repository only gives you `compose.yaml`,
`compose.local.yaml`, `.env.example` and the host firewall in `host/`. Keep it in `/opt/dockyard`:
the firewall service reads its settings from `/opt/dockyard/.env`.

### 2. Choose the ways in

The first two lines of `.env` pick them: the compose files and profiles Docker uses.

| Installation | `COMPOSE_FILE` | `COMPOSE_PROFILES` | Steps |
| --- | --- | --- | --- |
| Cloudflare only (the default) | `compose.yaml` | `cloudflare` | 3, 4 |
| Local sign-in only | `compose.yaml:compose.local.yaml` | *(empty)* | 5 |
| Both | `compose.yaml:compose.local.yaml` | `cloudflare` | 3, 4, 5 |

Follow the steps of your installation, then go on from step 6. Without Cloudflare, leave its three
settings in `.env` empty; Dockyard refuses to start when neither way in is set up.

### 3. Create the Cloudflare Tunnel

In the Cloudflare dashboard, open **Zero Trust → Networks → Tunnels**:

1. **Create a tunnel**, type **Cloudflared**, and give it a name (e.g. `dockyard`).
2. On the install page, copy the token: the long value after `--token` in any of the install
   commands. Put it in `.env` as `DOCKYARD_CF_TUNNEL_TOKEN`. Do not run the install command:
   `compose.yaml` already runs the tunnel client.
3. Add a **public hostname** (a *published application route* in newer dashboards), e.g.
   `dockyard.example.com`, with service type **HTTP** and URL **`dockyard:8000`**.

### 4. Protect it with Cloudflare Access

Still in Zero Trust, open **Access → Applications → Add an application → Self-hosted**:

1. Use the same hostname as the tunnel (`dockyard.example.com`).
2. Add a policy with action **Allow** that includes the emails (or the email domain) of the
   people who will use Dockyard. Access lets them through; Dockyard still admits only the people
   an Administrator invited.
3. Save, then open the application's **Overview** and copy the **Application Audience (AUD)
   Tag** into `.env` as `DOCKYARD_CF_ACCESS_AUD`.
4. Put your team name in `DOCKYARD_CF_ACCESS_TEAM`: the `acme` of your team domain
   `acme.cloudflareaccess.com` (shown under **Settings → Custom pages**, or in the address bar of
   the Access login page).

Dockyard verifies the token Cloudflare Access adds to every request and WebSocket: reaching
Dockyard any other way gives nothing.

### 5. Set up Local sign-in

Put the host's IPv4 address on your LAN in `.env` as `DOCKYARD_LOCAL_ADDRESS`, e.g.
`192.168.1.10` (`ip -4 addr` lists them). The sign-in page answers only there, on port 8443
(`DOCKYARD_LOCAL_HOST_PORT` changes it), and never on IPv6. Give the host a fixed address (static,
or a reservation on your router): Docker cannot start Dockyard while the address is missing.

`.env.example` explains the optional settings: the Local networks people may sign in from
(`DOCKYARD_LOCAL_NETWORKS`, by default the private ranges and the Tailscale range), and the
address Activation links point to (`DOCKYARD_LOCAL_URL`, e.g. `https://dockyard.lan:8443`).

To publish the sign-in page on one more address, e.g. the host's Tailscale address, create
`compose.override.yaml` next to `compose.yaml`:

```yaml
services:
  dockyard:
    ports:
      - "100.101.102.103:${DOCKYARD_LOCAL_HOST_PORT:-8443}:8443"
```

and add it at the end of `COMPOSE_FILE` in `.env`:
`COMPOSE_FILE=compose.yaml:compose.local.yaml:compose.override.yaml`. With `COMPOSE_FILE` set,
Docker does not read `compose.override.yaml` on its own.

### 6. Fill in the rest of `.env`

```sh
openssl rand -base64 32 | tr '+/' '-_'
```

- `DOCKYARD_SECRET_KEY`: the value printed above. It encrypts the Git Credentials and Second
  factors stored in Dockyard; keep a copy somewhere safe, since a restored Instance needs the same
  key.
- `DOCKYARD_FIRST_ADMINISTRATOR_EMAIL`: your email, the one you sign in with through Cloudflare
  Access or with Local sign-in.

`.env.example` explains the optional settings: the Instance limits (CPU and RAM per Project,
Active Projects per User), the DNS servers Projects use, the LAN addresses Projects may reach, and
the Dockyard release to run (`DOCKYARD_VERSION`, default `latest`).

### 7. Start Dockyard

```sh
docker compose up -d
docker compose logs -f dockyard
```

Dockyard refuses to start until every required setting is filled in, and its log says which one
is missing. At startup it creates the Projects network (bridge `dockyard0`) if it is missing, and
logs which ways in are on: `Cloudflare Access: on`, `Local sign-in: on, port 8443, published on
192.168.1.10`.

With Local sign-in, Dockyard also logs the certificate of its sign-in page and its fingerprint:
`Local sign-in certificate (self-signed), SHA-256 fingerprint: AB:CD:…`. Unless you provide one,
Dockyard makes a self-signed certificate at first start and keeps it in its data volume, across
restarts and updates. Browsers warn about it: before going on, check that the SHA-256 fingerprint
the browser shows for the certificate is the one in the log. To use your own certificate instead,
copy it and its key (PEM, the key without a passphrase) into the data volume and restart:

```sh
docker compose exec -T dockyard sh -c 'mkdir -p /data/tls && cat > /data/tls/cert.pem' < cert.pem
docker compose exec -T dockyard sh -c 'cat > /data/tls/key.pem' < key.pem
docker compose restart dockyard
```

### 8. Install the host firewall

Do this once Dockyard has started, so the Projects network exists:

```sh
sudo install -m 755 host/dockyard-firewall.sh /usr/local/sbin/dockyard-firewall
sudo install -m 644 host/dockyard-firewall.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now dockyard-firewall
```

From then on, Projects reach the internet but not the private ranges (10/8, 172.16/12,
192.168/16, 169.254/16, 100.64/10) nor the host. The service applies the rules at boot and every
time Docker starts, so Projects never run without them.

To let Projects reach a specific service on your LAN, list its address or range in `.env`
(`DOCKYARD_ALLOWED_IPS=192.168.1.50,10.0.5.0/24`), then
`sudo systemctl restart dockyard-firewall`.

To check: in the Terminal of a Project, `curl -m 3 http://192.168.1.1` (your router) must fail and
`curl -I https://github.com` must succeed.

### 9. Sign in as the first Administrator

**Through Cloudflare**: open `https://dockyard.example.com`. Cloudflare Access asks who you are;
with the email in `DOCKYARD_FIRST_ADMINISTRATOR_EMAIL` you land on your dashboard as
Administrator.

**With Local sign-in**: first give yourself a password. The command asks for it twice, without
showing it, so it never ends up in `.env` or in your shell history; it must be at least 12
characters long and not your email:

```sh
docker compose exec dockyard python -m dockyard set-password you@example.com
```

Then open `https://192.168.1.10:8443` from a device on your LAN and sign in. The same command
works for any User, as an emergency reset: it signs them out everywhere. For a User who lost both
the phone with their Second factor and their recovery codes,
`docker compose exec dockyard python -m dockyard remove-second-factor <email>` removes it.

From your dashboard:

1. **Connect your Claude Account** with your claude.ai subscription: Dockyard opens a terminal
   where you open the sign-in link and paste back the code.
2. **Invite** the other people under **Users**, by their email. Through Cloudflare, they must also
   be allowed by the Access policy. With Local sign-in, each Invitation comes with an Activation
   link, valid for 7 days and only once, that you copy from the Users page: the person opens it on
   the LAN and chooses their password. Dockyard sends no message: hand them the address, and the
   link. An Activation link is only as safe as the channel you hand it over on.
3. Create a Project from a repository URL. It appears in the Claude app (Code tab) under the name
   you gave it.

### 10. Check that the sign-in page is unreachable from outside

With Local sign-in, check where Docker publishes the port and that nothing else answers:

1. On the host, `docker compose ps dockyard` must list under PORTS only the addresses you named
   (`192.168.1.10:8443->8443/tcp`), never `0.0.0.0` or `[::]`.
2. From a device outside your networks, e.g. a phone on mobile data and off the VPN,
   `https://<the host's public address>:8443` must not open. If the host has an IPv6 address,
   `curl -k -m 5 "https://[<that address>]:8443"` must fail too.
3. From a device on the LAN, `https://192.168.1.10:8443` shows the sign-in page.

Dockyard checks the client too: a request on the local port from outside the Local networks, or
from Docker's own networks, gets a refusal page that names nothing of the Instance.

## Update

**An Instance installed before Local sign-in must first add this line to `.env`**, or the tunnel
no longer starts and Cloudflare stops working:

```sh
COMPOSE_PROFILES=cloudflare
```

Then run `git pull`, since this release changes `compose.yaml` and `.env.example`, and update as
usual:

```sh
cd /opt/dockyard
docker compose pull
docker compose up -d
```

`docker compose pull` fetches the newest Dockyard image and Base Image of your release. The Base
Image is rebuilt every week, so Claude Code in it stays current: when a newer one arrives,
Dockyard builds every Image of the Instance again, and each Project shows "update available" and
moves to it at its next restart. Only the latest release gets this weekly rebuild: an Instance
pinned to an older one with `DOCKYARD_VERSION` keeps the Claude Code it last pulled.

When a release changes `compose.yaml`, `.env.example` or the host firewall, its notes say so: run
`git pull` first, then reinstall the firewall as in step 8 if it changed.

## Backups

Dockyard does not back itself up. Everything lives in Docker volumes: `dockyard_dockyard-data`
(Users, Invitations, Git Credentials, Images, Projects) and, per Project and User, the volumes of
code, home and Claude Account. Back them up with your usual tools, together with `.env`.

## Development

```sh
uv sync && uv run playwright install chromium
uv run pytest tests/api tests/e2e          # Seams A and C: in-memory Runtime, no Docker needed
uv run pytest tests/runtime                # Seam B: the Runtime contract, also against Docker
sudo DOCKYARD_FIREWALL_TESTS=1 DOCKYARD_LOCAL_PORT_TESTS=1 .venv/bin/pytest tests/host  # Host: root, Linux
docker compose -f compose.yaml -f compose.dev.yaml up -d --build   # run this checkout
```

A version tag (`git tag v1.2.3 && git push --tags`) publishes the Dockyard image and the Base
Image to GHCR once CI passes; the Release workflow also rebuilds the Base Image of the latest
release every Monday (GitHub turns scheduled workflows off after 60 days without activity in the
repository; re-enable it from the Actions tab).

## Licence

Apache License 2.0: see [LICENSE](LICENSE).
