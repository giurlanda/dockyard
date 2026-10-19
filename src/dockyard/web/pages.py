"""Server-rendered pages, in the language of whoever asks."""

from __future__ import annotations

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape
from starlette.requests import HTTPConnection
from starlette.responses import HTMLResponse, JSONResponse, Response

from .guard import Refusal, Refused, is_api
from .i18n import LANGUAGE_NAMES, language_for, translator


class Pages:
    def __init__(self, *, local_sign_in: bool = False) -> None:
        self._local_sign_in = local_sign_in  # the pages of the local listener
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
            local_sign_in=self._local_sign_in,
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
        key = f"refused.{refused.reason}"
        if refused.reason == Refusal.UNAUTHENTICATED and self._local_sign_in:
            key = "local_sign_in.required"  # here, not at an authentication proxy
        return self.problem(
            conn,
            refused.reason,
            key=key,
            status_code=refused.reason.status_code,
            email=refused.email,
        )


def same_site_path(target: str) -> str:
    """Only paths on this site: never an open redirect."""
    if target.startswith("/") and not target.startswith(("//", "/\\")):
        return target
    return "/"
