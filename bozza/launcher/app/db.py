"""Archivio SQLite dei progetti. Lo stato dei container si legge sempre da Docker."""
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from cryptography.fernet import Fernet

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    repo_url        TEXT NOT NULL,
    provider        TEXT NOT NULL,
    token_enc       BLOB,
    permission_mode TEXT NOT NULL,
    cpu_limit       REAL NOT NULL,
    mem_limit_mb    INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);
"""

PUBLIC_FIELDS = ("id", "name", "repo_url", "provider", "permission_mode",
                 "cpu_limit", "mem_limit_mb", "created_at")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with closing(_conn()) as conn, conn:
        conn.executescript(SCHEMA)


def _fernet() -> Fernet:
    return Fernet(config.SECRET_KEY.encode())


def encrypt(value: str) -> bytes:
    return _fernet().encrypt(value.encode())


def decrypt(blob: bytes) -> str:
    return _fernet().decrypt(blob).decode()


def public(row: sqlite3.Row) -> dict:
    """Il token cifrato non esce mai dalle API."""
    data = {k: row[k] for k in PUBLIC_FIELDS}
    data["has_token"] = row["token_enc"] is not None
    return data


def insert_project(p: dict) -> None:
    with closing(_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO projects (id, name, repo_url, provider, token_enc, permission_mode,"
            " cpu_limit, mem_limit_mb, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (p["id"], p["name"], p["repo_url"], p["provider"], p.get("token_enc"),
             p["permission_mode"], p["cpu_limit"], p["mem_limit_mb"],
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )


def list_projects() -> list[sqlite3.Row]:
    with closing(_conn()) as conn:
        return conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()


def get_project(pid: str) -> sqlite3.Row | None:
    with closing(_conn()) as conn:
        return conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()


def delete_project(pid: str) -> None:
    with closing(_conn()) as conn, conn:
        conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
