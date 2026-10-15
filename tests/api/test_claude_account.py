"""A User connects their Claude Account once, through a login terminal in the browser."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from dockyard.runtime.fake import FakeRuntime

from .conftest import ADMINISTRATOR, AccessProxy

LOGIN_TERMINAL = "/ws/claude-account/login"
OTHER_USER = "another.user@example.com"


def test_a_user_who_never_connected_sees_their_claude_account_is_not_connected(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    page = dockyard.get("/", headers=proxy.headers(ADMINISTRATOR))

    assert "Claude Account" in page.text
    assert "Not connected" in page.text
    assert "Connect" in page.text


def test_the_api_tells_a_user_whether_their_claude_account_is_connected(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    status = dockyard.get("/api/claude-account", headers=proxy.headers(ADMINISTRATOR))

    assert status.status_code == 200
    assert status.json() == {"connected": False, "login_in_progress": False}


def test_the_dashboard_warns_against_api_keys_and_long_lived_tokens(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    page = dockyard.get("/", headers=proxy.headers(ADMINISTRATOR))

    assert "API key" in page.text
    assert "long-lived token" in page.text
    assert "Remote Control" in page.text


def test_a_user_connects_their_claude_account_through_the_login_terminal(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    started = dockyard.post("/api/claude-account/login", headers=signed_in)
    assert started.status_code == 202
    assert started.json() == {"connected": False, "login_in_progress": True}

    with dockyard.websocket_connect(LOGIN_TERMINAL, headers=signed_in) as terminal:
        receive_until(terminal, "oauth/authorize")
        terminal.send_json({"type": "input", "data": "the-code-from-claude-ai\r"})
        receive_until(terminal, "Login successful.")
        with pytest.raises(WebSocketDisconnect) as ended:
            receive_until(terminal, "the login program never prints this")
    assert ended.value.code == 1000

    status = dockyard.get("/api/claude-account", headers=signed_in)
    assert status.json() == {"connected": True, "login_in_progress": False}
    page = dockyard.get("/", headers=signed_in)
    assert "Not connected" not in page.text
    assert "Reconnect" in page.text


def test_resize_messages_reach_the_login_terminal(dockyard: TestClient, proxy: AccessProxy) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    dockyard.post("/api/claude-account/login", headers=signed_in)

    with dockyard.websocket_connect(LOGIN_TERMINAL, headers=signed_in) as terminal:
        receive_until(terminal, "oauth/authorize")
        terminal.send_json({"type": "resize", "cols": 100, "rows": 30})
        terminal.send_json({"type": "input", "data": "the-code-from-claude-ai\r"})

        assert "Terminal size: 30 100" in receive_until(terminal, "Login successful.")


def test_the_login_terminal_is_closed_when_no_sign_in_is_in_progress(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        dockyard.websocket_connect(
            LOGIN_TERMINAL, headers=proxy.headers(ADMINISTRATOR)
        ) as terminal,
    ):
        terminal.receive_bytes()

    assert closed.value.code == 4404


def test_a_user_never_reaches_another_users_login_terminal_or_claude_account(
    dockyard: TestClient, proxy: AccessProxy, add_user: Callable[[str], None]
) -> None:
    add_user(OTHER_USER)
    dockyard.post("/api/claude-account/login", headers=proxy.headers(ADMINISTRATOR))

    with (
        pytest.raises(WebSocketDisconnect) as closed,
        dockyard.websocket_connect(LOGIN_TERMINAL, headers=proxy.headers(OTHER_USER)) as terminal,
    ):
        terminal.receive_bytes()
    assert closed.value.code == 4404

    with dockyard.websocket_connect(
        LOGIN_TERMINAL, headers=proxy.headers(ADMINISTRATOR)
    ) as terminal:
        receive_until(terminal, "oauth/authorize")
        terminal.send_json({"type": "input", "data": "the-code-from-claude-ai\r"})
        receive_until(terminal, "Login successful.")
    other = dockyard.get("/api/claude-account", headers=proxy.headers(OTHER_USER))
    assert other.json() == {"connected": False, "login_in_progress": False}


def test_cancelling_a_sign_in_closes_the_login_terminal(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    dockyard.post("/api/claude-account/login", headers=signed_in)

    with dockyard.websocket_connect(LOGIN_TERMINAL, headers=signed_in) as terminal:
        receive_until(terminal, "oauth/authorize")
        cancelled = dockyard.delete("/api/claude-account/login", headers=signed_in)
        with pytest.raises(WebSocketDisconnect) as closed:
            receive_until(terminal, "the login program never prints this")

    assert cancelled.status_code == 204
    assert closed.value.code == 1000
    status = dockyard.get("/api/claude-account", headers=signed_in)
    assert status.json() == {"connected": False, "login_in_progress": False}


def test_connecting_from_the_dashboard_opens_the_login_page(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)

    page = dockyard.post("/claude-account/login", headers=signed_in)

    assert page.url.path == "/claude-account/login"
    assert "Connect your Claude Account" in page.text
    assert "Open the link shown in the terminal" in page.text
    assert "API key" in page.text
    status = dockyard.get("/api/claude-account", headers=signed_in)
    assert status.json()["login_in_progress"] is True


def test_the_dashboard_offers_to_continue_a_sign_in_in_progress(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    dockyard.post("/api/claude-account/login", headers=signed_in)

    page = dockyard.get("/", headers=signed_in)

    assert "Continue signing in" in page.text


def test_the_login_page_leads_back_to_the_dashboard_when_no_sign_in_is_in_progress(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    page = dockyard.get(
        "/claude-account/login", headers=proxy.headers(ADMINISTRATOR), follow_redirects=False
    )

    assert page.status_code == 303
    assert page.headers["location"] == "/"


def test_cancelling_from_the_login_page_returns_to_the_dashboard(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    dockyard.post("/claude-account/login", headers=signed_in)

    page = dockyard.post("/claude-account/login/cancel", headers=signed_in)

    assert page.url.path == "/"
    assert "Continue signing in" not in page.text
    assert "Not connected" in page.text


def test_the_login_page_speaks_italian(dockyard: TestClient, proxy: AccessProxy) -> None:
    page = dockyard.post(
        "/claude-account/login", headers={**proxy.headers(ADMINISTRATOR), "Accept-Language": "it"}
    )

    assert '<html lang="it">' in page.text
    assert "Collega il tuo Account Claude" in page.text
    assert "chiave API" in page.text


def receive_until(terminal: WebSocketTestSession, text: str) -> str:
    """What the login terminal shows, up to `text`."""
    shown = ""
    while text not in shown:
        shown += terminal.receive_bytes().decode(errors="replace")
    return shown


def test_users_are_told_when_dockyard_cannot_reach_docker(
    dockyard: TestClient, proxy: AccessProxy, runtime: FakeRuntime
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)

    with runtime.unavailable():
        page = dockyard.get("/", headers=signed_in)
        status = dockyard.get("/api/claude-account", headers=signed_in)
        with (
            pytest.raises(WebSocketDisconnect) as closed,
            dockyard.websocket_connect(LOGIN_TERMINAL, headers=signed_in) as terminal,
        ):
            terminal.receive_bytes()

    assert page.status_code == 503
    assert "cannot reach Docker" in page.text
    assert status.status_code == 503
    assert status.json()["error"] == "runtime_unavailable"
    assert closed.value.code == 4503
