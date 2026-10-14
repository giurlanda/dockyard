"""Tutte le operazioni su Docker, tramite il socket proxy (DOCKER_HOST)."""
import re
from urllib.parse import urlparse

import docker
from docker.errors import NotFound

from . import config

_client: docker.DockerClient | None = None

# Applicato a ogni container creato dal launcher
HARDENING = dict(
    cap_drop=["ALL"],
    security_opt=["no-new-privileges:true"],
    pids_limit=2048,
)

_PATH_RE = re.compile(r"^/[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+$")


class LauncherError(Exception):
    """Errore mostrabile all'utente."""


def client() -> docker.DockerClient:
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


# ---------- nomi ----------

def container_name(pid: str) -> str:
    return f"proj-{pid}"


def workspace_volume(pid: str) -> str:
    return f"proj-{pid}-workspace"


# ---------- validazione ----------

def parse_repo_url(url: str) -> tuple[str, str]:
    """Ritorna (url normalizzato, provider). Solo https, solo host ammessi."""
    url = url.strip()
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or u.username or u.password or u.query or u.fragment or u.port:
        raise LauncherError("Usa un URL https semplice, senza credenziali, porta o parametri.")
    if host not in config.ALLOWED_GIT_HOSTS:
        raise LauncherError(f"Host non ammesso: {host}. Aggiungilo ad ALLOWED_GIT_HOSTS.")
    path = u.path.rstrip("/")
    if ".." in path or not _PATH_RE.match(path):
        raise LauncherError("Percorso del repository non valido.")
    provider = "github" if host == "github.com" else "gitlab"
    return f"https://{host}{path}", provider


# ---------- infrastruttura ----------

def ensure_infra() -> None:
    """Crea rete dei progetti e volume della config Claude se mancano."""
    c = client()
    try:
        c.networks.get(config.PROJECTS_NETWORK)
    except NotFound:
        c.networks.create(
            config.PROJECTS_NETWORK,
            driver="bridge",
            labels={"launcher.managed": "1"},
            options={
                # nome fisso dell'interfaccia: lo usano le regole iptables dell'host
                "com.docker.network.bridge.name": config.PROJECTS_BRIDGE,
                # i progetti non si vedono tra loro
                "com.docker.network.bridge.enable_icc": "false",
            },
        )
    try:
        c.volumes.get(config.CLAUDE_CONFIG_VOLUME)
    except NotFound:
        c.volumes.create(config.CLAUDE_CONFIG_VOLUME, labels={"launcher.managed": "1"})


# ---------- clone ----------

_CLONE_SCRIPT = r"""set -eu
cd /workspace
if [ -n "${GIT_TOKEN:-}" ]; then
  AUTH=$(printf '%s:%s' "$GIT_USER" "$GIT_TOKEN" | base64 -w0)
  # l'header vale solo per questo comando: il token non finisce in .git/config
  git -c "http.extraHeader=Authorization: Basic $AUTH" clone -- "$REPO_URL" repo
else
  git clone -- "$REPO_URL" repo
fi
"""


def clone_repo(pid: str, repo_url: str, provider: str, token: str | None) -> None:
    """Clona il repo nel volume del progetto con un container effimero."""
    c = client()
    vol = workspace_volume(pid)
    c.volumes.create(vol, labels={config.LABEL_PROJECT: pid})

    env = {"REPO_URL": repo_url}
    if token:
        env["GIT_TOKEN"] = token
        env["GIT_USER"] = "x-access-token" if provider == "github" else "oauth2"

    ctr = c.containers.run(
        config.PROJECT_IMAGE,
        ["bash", "-c", _CLONE_SCRIPT],
        environment=env,
        volumes={vol: {"bind": config.WORKSPACE, "mode": "rw"}},
        network=config.PROJECTS_NETWORK,
        labels={config.LABEL_JOB: "clone"},
        detach=True,
        **HARDENING,
    )
    try:
        result = ctr.wait(timeout=900)
        output = ctr.logs(tail=30).decode(errors="replace")
    finally:
        ctr.remove(force=True)  # rimuove anche le variabili d'ambiente con il token

    if result.get("StatusCode") != 0:
        remove_volume(pid)
        raise LauncherError("Clone fallito:\n" + output)


# ---------- container di progetto ----------

def start_project(p) -> None:
    """Avvia il container del progetto, creandolo se non esiste."""
    c = client()
    name = container_name(p["id"])
    try:
        c.containers.get(name).start()
        return
    except NotFound:
        pass

    cmd = [
        "claude", "remote-control",
        "--name", p["name"],
        "--spawn", "worktree",
        "--permission-mode", p["permission_mode"],
    ]
    c.containers.run(
        config.PROJECT_IMAGE,
        cmd,
        name=name,
        hostname=name,
        working_dir=config.REPO_DIR,
        volumes={
            workspace_volume(p["id"]): {"bind": config.WORKSPACE, "mode": "rw"},
            config.CLAUDE_CONFIG_VOLUME: {"bind": config.CLAUDE_HOME, "mode": "rw"},
        },
        network=config.PROJECTS_NETWORK,
        labels={config.LABEL_PROJECT: p["id"]},
        mem_limit=f"{int(p['mem_limit_mb'])}m",
        nano_cpus=int(float(p["cpu_limit"]) * 1_000_000_000),
        restart_policy={"Name": "unless-stopped"},
        # TTY: claude remote-control può fare domande (trust della cartella)
        tty=True,
        stdin_open=True,
        detach=True,
        **HARDENING,
    )


def _get(pid: str):
    try:
        return client().containers.get(container_name(pid))
    except NotFound:
        raise LauncherError("Container non trovato: avvia il progetto.")


def stop_project(pid: str) -> None:
    _get(pid).stop(timeout=20)


def restart_project(pid: str) -> None:
    _get(pid).restart(timeout=20)


def remove_project(pid: str, delete_volume: bool) -> None:
    try:
        client().containers.get(container_name(pid)).remove(force=True)
    except NotFound:
        pass
    if delete_volume:
        remove_volume(pid)


def remove_volume(pid: str) -> None:
    try:
        client().volumes.get(workspace_volume(pid)).remove(force=True)
    except NotFound:
        pass


def statuses() -> dict[str, str]:
    """{project_id: stato} per tutti i container di progetto."""
    out = {}
    for ctr in client().containers.list(all=True, filters={"label": config.LABEL_PROJECT}):
        pid = ctr.labels.get(config.LABEL_PROJECT)
        if pid:
            out[pid] = ctr.status
    return out


def stats(pid: str) -> dict:
    ctr = _get(pid)
    if ctr.status != "running":
        return {"cpu_percent": 0.0, "mem_mb": 0, "mem_limit_mb": 0}
    s = ctr.stats(stream=False)
    cpu, pre = s.get("cpu_stats", {}), s.get("precpu_stats", {})
    cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - pre.get("cpu_usage", {}).get("total_usage", 0)
    sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
    ncpu = cpu.get("online_cpus") or 1
    mem = s.get("memory_stats", {})
    return {
        "cpu_percent": round(cpu_delta / sys_delta * ncpu * 100, 1) if sys_delta > 0 else 0.0,
        "mem_mb": round(mem.get("usage", 0) / 1_048_576),
        "mem_limit_mb": round(mem.get("limit", 0) / 1_048_576),
    }


def log_stream(pid: str):
    """Stream dei log (oggetto con .close() per interromperlo)."""
    return _get(pid).logs(stream=True, follow=True, tail=300)


# ---------- terminali ----------

def _raw(sock):
    return getattr(sock, "_sock", sock)


def open_shell(pid: str):
    """Shell interattiva nel container. Ritorna (resize_fn, socket)."""
    ctr = _get(pid)
    if ctr.status != "running":
        raise LauncherError("Il progetto non è in esecuzione.")
    api = client().api
    exec_id = api.exec_create(
        ctr.id, ["bash", "-l"], stdin=True, tty=True, user="ubuntu",
        workdir=config.REPO_DIR, environment={"TERM": "xterm-256color"},
    )["Id"]
    sock = api.exec_start(exec_id, socket=True, tty=True)
    return (lambda rows, cols: api.exec_resize(exec_id, height=rows, width=cols)), _raw(sock)


def attach_console(name: str):
    """Collega al processo principale (claude remote-control o login)."""
    api = client().api
    try:
        ctr = client().containers.get(name)
    except NotFound:
        raise LauncherError("Container non trovato.")
    if ctr.status != "running":
        raise LauncherError("Il container non è in esecuzione.")
    sock = api.attach_socket(ctr.id, params={"stdin": 1, "stdout": 1, "stderr": 1, "stream": 1, "logs": 1})
    return (lambda rows, cols: api.resize(ctr.id, height=rows, width=cols)), _raw(sock)


# ---------- login Claude ----------

_LOGIN_SCRIPT = (
    "echo; echo '== Login Claude =='; "
    "echo 'Apri il link nel browser, poi incolla qui il codice.'; echo; "
    "claude auth login; "
    "echo; echo 'Fatto. Puoi chiudere questa finestra.'; exec bash -l"
)


def start_login() -> None:
    c = client()
    stop_login()
    c.containers.run(
        config.PROJECT_IMAGE,
        ["bash", "-lc", _LOGIN_SCRIPT],
        name=config.LOGIN_CONTAINER,
        volumes={config.CLAUDE_CONFIG_VOLUME: {"bind": config.CLAUDE_HOME, "mode": "rw"}},
        network=config.PROJECTS_NETWORK,
        labels={config.LABEL_JOB: "login"},
        tty=True,
        stdin_open=True,
        detach=True,
        **HARDENING,
    )


def stop_login() -> None:
    try:
        client().containers.get(config.LOGIN_CONTAINER).remove(force=True)
    except NotFound:
        pass


def claude_logged_in() -> bool:
    """Euristica: il file delle credenziali esiste nel volume config.

    Su Linux Claude Code salva le credenziali in <config dir>/.credentials.json;
    se in futuro cambia, adatta questo controllo.
    """
    try:
        client().containers.run(
            config.PROJECT_IMAGE,
            ["test", "-s", f"{config.CLAUDE_HOME}/.credentials.json"],
            volumes={config.CLAUDE_CONFIG_VOLUME: {"bind": config.CLAUDE_HOME, "mode": "ro"}},
            network_disabled=True,
            labels={config.LABEL_JOB: "check"},
            remove=True,
            **HARDENING,
        )
        return True
    except docker.errors.ContainerError:
        return False
