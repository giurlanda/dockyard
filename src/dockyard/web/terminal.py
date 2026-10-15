"""The WebSocket bridge between a terminal in the browser and a program's TTY in the Runtime.

Browser to Dockyard: JSON text messages, {"type": "input", "data": "..."} for keys and
{"type": "resize", "cols": 80, "rows": 24} for the size. Dockyard to browser: the terminal's raw
bytes. Dockyard closes the socket (1000) when the TTY ends.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

import anyio
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from starlette.websockets import WebSocket, WebSocketDisconnect

from ..runtime import Tty

log = logging.getLogger(__name__)


class Input(BaseModel):
    type: Literal["input"]
    data: str


class Resize(BaseModel):
    type: Literal["resize"]
    cols: int = Field(ge=1, le=1000)
    rows: int = Field(ge=1, le=1000)


ClientMessage: TypeAdapter[Input | Resize] = TypeAdapter(
    Annotated[Input | Resize, Field(discriminator="type")]
)


async def bridge(websocket: WebSocket, tty: Tty) -> None:
    """Join an accepted WebSocket to a TTY until either side ends, then let go of both."""

    async def to_browser() -> None:
        while data := await anyio.to_thread.run_sync(tty.read, abandon_on_cancel=True):
            await websocket.send_bytes(data)
        await websocket.close()

    async def from_browser() -> None:
        while (message := await websocket.receive())["type"] == "websocket.receive":
            try:
                match ClientMessage.validate_json(message.get("text") or ""):
                    case Input(data=data):
                        await anyio.to_thread.run_sync(tty.write, data.encode())
                    case Resize(rows=rows, cols=cols):
                        await anyio.to_thread.run_sync(tty.resize, rows, cols)
            except ValidationError:
                log.debug("Ignored a terminal message that is neither input nor resize")

    try:
        async with anyio.create_task_group() as tasks:
            for side in (to_browser, from_browser):
                tasks.start_soon(_until_either_ends, side, tasks.cancel_scope)
    finally:
        tty.close()


async def _until_either_ends(side: Callable[[], Awaitable[None]], both: anyio.CancelScope) -> None:
    try:
        await side()
    except WebSocketDisconnect:
        pass
    finally:
        both.cancel()
