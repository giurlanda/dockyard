"""Server-rendered pages, in the language of whoever asks."""

from __future__ import annotations

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape
from starlette.requests import HTTPConnection
from starlette.responses import HTMLResponse, JSONResponse, Response

from .guard import Refusal, Refused
from .i18n import LANGUAGE_NAMES, language_for, translator


class Pages:
    def __init__(self) -> None:
        self._env = Environment(
            loader=PackageLoader("dockyard.web", "templates"),
            autoescape=select_autoescape(),
            undefined=StrictUndefined,
        )

    def render(
        self, conn: HTTPConnection, template: str, *, status_code: int = 200, **context: object
    ) -> HTMLResponse:
        language = language_for(conn)
        html = self._env.get_template(template).render(
            lang=language,
            t=translator(language),
            languages=LANGUAGE_NAMES,
            path=conn.url.path,
            **context,
        )
        return HTMLResponse(html, status_code=status_code)

    def notice(
        self,
        conn: HTTPConnection,
        key: str,
        *,
        status_code: int,
        home: bool = False,
        **params: object,
    ) -> HTMLResponse:
        """A page that only explains something: why a request was refused, a missing page..."""
        return self.render(
            conn, "notice.html", status_code=status_code, key=key, home=home, params=params
        )

    def problem(
        self, conn: HTTPConnection, error: str, *, key: str, status_code: int, **params: object
    ) -> Response:
        """Why a request failed: {"error", "message"} for the API, a notice page for people."""
        if is_api(conn):
            message = translator(language_for(conn))(f"{key}.body", **params)
            return JSONResponse({"error": error, "message": message}, status_code)
        return self.render(
            conn, "notice.html", status_code=status_code, key=key, home=False, params=params
        )

    def refusal(self, conn: HTTPConnection, refused: Refused) -> Response:
        if refused.reason == Refusal.OUTSIDE_LOCAL_NETWORKS and not is_api(conn):
            # A page that names nothing of the Instance, not even what it is.
            return self.render(conn, "unreachable.html", status_code=refused.reason.status_code)
        return self.problem(
            conn,
            refused.reason,
            key=f"refused.{refused.reason}",
            status_code=refused.reason.status_code,
            email=refused.email,
        )


def is_api(conn: HTTPConnection) -> bool:
    return conn.url.path.startswith("/api/")
