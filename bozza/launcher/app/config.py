"""Configurazione letta dalle variabili d'ambiente (vedi .env.example)."""
import os
from pathlib import Path


def _flag(name: str) -> bool:
    return os.getenv(name, "0").strip().lower() in ("1", "true", "yes")


DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "launcher.db"

CF_ACCESS_TEAM = os.getenv("CF_ACCESS_TEAM", "").strip()
CF_ACCESS_AUD = os.getenv("CF_ACCESS_AUD", "").strip()
ALLOWED_EMAIL = os.getenv("ALLOWED_EMAIL", "").strip().lower()
DEV_NO_AUTH = _flag("DEV_NO_AUTH")

SECRET_KEY = os.getenv("LAUNCHER_SECRET_KEY", "").strip()

ALLOWED_GIT_HOSTS = {
    h.strip().lower()
    for h in os.getenv("ALLOWED_GIT_HOSTS", "github.com,gitlab.com").split(",")
    if h.strip()
}
PROJECT_IMAGE = os.getenv("PROJECT_IMAGE", "claude-project:latest")

# Nomi delle risorse Docker gestite dal launcher
PROJECTS_NETWORK = "projects-net"
PROJECTS_BRIDGE = "br-projects"          # nome dell'interfaccia sull'host, usato da host/block-lan.sh
CLAUDE_CONFIG_VOLUME = "claude-config"
LOGIN_CONTAINER = "claude-login"
LABEL_PROJECT = "launcher.project"
LABEL_JOB = "launcher.job"

# Percorsi dentro l'immagine di progetto
CLAUDE_HOME = "/home/ubuntu/.claude"
WORKSPACE = "/workspace"
REPO_DIR = "/workspace/repo"

PERMISSION_MODES = ("default", "acceptEdits", "auto", "bypassPermissions")


def check() -> None:
    """Ferma l'avvio se manca qualcosa di essenziale."""
    missing = []
    if not SECRET_KEY:
        missing.append("LAUNCHER_SECRET_KEY")
    if not DEV_NO_AUTH:
        for name, value in (("CF_ACCESS_TEAM", CF_ACCESS_TEAM),
                            ("CF_ACCESS_AUD", CF_ACCESS_AUD),
                            ("ALLOWED_EMAIL", ALLOWED_EMAIL)):
            if not value:
                missing.append(name)
    if missing:
        raise RuntimeError("Variabili mancanti: " + ", ".join(missing))
