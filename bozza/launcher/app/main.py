"""Claude Code Launcher: API, dashboard e terminali web."""
import asyncio
import json
import re
import secrets
import socket
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import config, db
from . import docker_ops as ops
from .auth import CloudflareAccessMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.check()
    db.init()
    await asyncio.to_thread(ops.ensure_infra)
    yield


app = FastAPI(title="Claude Code Launcher", lifespan=lifespan)
app.add_middleware(CloudflareAccessMiddleware)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _err(e: ops.LauncherError):
    return HTTPException(status_code=400, detail=str(e))


def _project_or_404(pid: str):
    row = db.get_project(pid)
    if row is None:
        raise HTTPException(status_code=404, detail="Progetto non trovato")
    return row


# ---------- pagine ----------

@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "permission_modes": config.PERMISSION_MODES,
        "git_hosts": sorted(config.ALLOWED_GIT_HOSTS),
    })


# ---------- progetti ----------

class ProjectIn(BaseModel):
    repo_url: str = Field(max_length=300)
    name: str = Field(min_length=1, max_length=60)
    token: str | None = Field(default=None, max_length=500)
    permission_mode: str = "default"
    cpu_limit: float = Field(default=2.0, ge=0.25, le=16)
    mem_limit_mb: int = Field(default=4096, ge=512, le=65536)


_NAME_RE = re.compile(r"^[\w .-]+$")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:20] or "progetto"
    return f"{s}-{secrets.token_hex(3)}"


@app.get("/api/projects")
def list_projects():
    status = ops.statuses()
    out = []
    for row in db.list_projects():
        p = db.public(row)
        p["status"] = status.get(p["id"], "missing")
        out.append(p)
    return out


@app.post("/api/projects", status_code=201)
def create_project(body: ProjectIn):
    if body.permission_mode not in config.PERMISSION_MODES:
        raise HTTPException(400, "Modalità permessi non valida")
    if not _NAME_RE.match(body.name):
        raise HTTPException(400, "Nome: solo lettere, numeri, spazi, punto, trattino")
    try:
        repo_url, provider = ops.parse_repo_url(body.repo_url)
        token = (body.token or "").strip() or None
        p = {
            "id": _slug(body.name),
            "name": body.name.strip(),
            "repo_url": repo_url,
            "provider": provider,
            "token_enc": db.encrypt(token) if token else None,
            "permission_mode": body.permission_mode,
            "cpu_limit": body.cpu_limit,
            "mem_limit_mb": body.mem_limit_mb,
        }
        ops.clone_repo(p["id"], repo_url, provider, token)
        db.insert_project(p)
        ops.start_project(p)
    except ops.LauncherError as e:
        raise _err(e)
    return {"id": p["id"]}


@app.post("/api/projects/{pid}/{action}")
def project_action(pid: str, action: str):
    row = _project_or_404(pid)
    try:
        if action == "start":
            ops.start_project(row)
        elif action == "stop":
            ops.stop_project(pid)
        elif action == "restart":
            ops.restart_project(pid)
        else:
            raise HTTPException(404, "Azione sconosciuta")
    except ops.LauncherError as e:
        raise _err(e)
    return {"ok": True}


@app.delete("/api/projects/{pid}")
def delete_project(pid: str, delete_volume: bool = False):
    _project_or_404(pid)
    ops.remove_project(pid, delete_volume)
    db.delete_project(pid)
    return {"ok": True}


@app.get("/api/projects/{pid}/stats")
def project_stats(pid: str):
    _project_or_404(pid)
    try:
        return ops.stats(pid)
    except ops.LauncherError as e:
        raise _err(e)


# ---------- login Claude ----------

@app.get("/api/claude/status")
def claude_status():
    return {"logged_in": ops.claude_logged_in()}


@app.post("/api/claude/login")
def claude_login_start():
    ops.start_login()
    return {"ok": True}


@app.delete("/api/claude/login")
def claude_login_stop():
    ops.stop_login()
    return {"ok": True}


# ---------- WebSocket: terminali e log ----------
# Protocollo client -> server: testo JSON
#   {"type": "input", "data": "..."}  |  {"type": "resize", "cols": 80, "rows": 24}
# Server -> client: byte grezzi del terminale.

async def _bridge(ws: WebSocket, resize, sock: socket.socket):
    loop = asyncio.get_running_loop()

    async def to_client():
        while True:
            data = await loop.run_in_executor(None, sock.recv, 65536)
            if not data:
                break
            await ws.send_bytes(data)

    async def from_client():
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("type") == "input":
                await loop.run_in_executor(None, sock.sendall, msg.get("data", "").encode())
            elif msg.get("type") == "resize":
                rows, cols = int(msg.get("rows", 24)), int(msg.get("cols", 80))
                await loop.run_in_executor(None, resize, rows, cols)

    tasks = [asyncio.create_task(to_client()), asyncio.create_task(from_client())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            t.cancel()
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        try:
            await ws.close()
        except RuntimeError:
            pass


async def _open_or_close(ws: WebSocket, opener):
    await ws.accept()
    try:
        resize, sock = await asyncio.to_thread(opener)
    except ops.LauncherError as e:
        await ws.send_bytes(f"\r\n[errore] {e}\r\n".encode())
        await ws.close()
        return
    await _bridge(ws, resize, sock)


@app.websocket("/ws/projects/{pid}/shell")
async def ws_shell(ws: WebSocket, pid: str):
    if db.get_project(pid) is None:
        return await ws.close(code=4404)
    await _open_or_close(ws, lambda: ops.open_shell(pid))


@app.websocket("/ws/projects/{pid}/console")
async def ws_console(ws: WebSocket, pid: str):
    if db.get_project(pid) is None:
        return await ws.close(code=4404)
    await _open_or_close(ws, lambda: ops.attach_console(ops.container_name(pid)))


@app.websocket("/ws/claude/login")
async def ws_login(ws: WebSocket):
    await _open_or_close(ws, lambda: ops.attach_console(config.LOGIN_CONTAINER))


@app.websocket("/ws/projects/{pid}/logs")
async def ws_logs(ws: WebSocket, pid: str):
    if db.get_project(pid) is None:
        return await ws.close(code=4404)
    await ws.accept()
    try:
        stream = await asyncio.to_thread(ops.log_stream, pid)
    except ops.LauncherError as e:
        await ws.send_bytes(f"\r\n[errore] {e}\r\n".encode())
        return await ws.close()

    loop = asyncio.get_running_loop()

    async def pump():
        while True:
            chunk = await loop.run_in_executor(None, next, stream, None)
            if chunk is None:
                break
            await ws.send_bytes(chunk)

    async def wait_close():
        while True:
            await ws.receive_text()  # ignora input; termina alla disconnessione

    tasks = [asyncio.create_task(pump()), asyncio.create_task(wait_close())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        stream.close()  # sblocca il thread fermo sul log
