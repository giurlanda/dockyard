"""Local sign-in on the local listener: email and password give a session, which then stands for
the User exactly as Cloudflare Access does on the tunnel listener."""

import time
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from starlette.websockets import WebSocketDisconnect

from dockyard.identity import SESSION_COOKIE
from dockyard.runtime.fake import FakeRuntime
from dockyard.web import Instance, ListenerName

from .conftest import ADMINISTRATOR, AccessProxy, FakeClock, listener_client
from .test_project_following import receive_until, running_project

LOCAL_SIGN_IN = {"DOCKYARD_LOCAL_PORT": "8443", "DOCKYARD_LOCAL_ADDRESS": "192.168.1.10"}
ON_THE_LAN = "192.168.1.20"
PASSWORD = "correct horse battery staple"
USER = "invited.user@example.com"


@pytest.fixture
def instance(start_instance: Callable[..., Instance]) -> Instance:
    """An Instance with both ways in, whose first Administrator was given a password."""
    started = start_instance(**LOCAL_SIGN_IN)
    started.local_sign_in.set_password(ADMINISTRATOR, PASSWORD)
    return started


@pytest.fixture
def local(instance: Instance) -> TestClient:
    return listener_client(instance, ListenerName.LOCAL, address=ON_THE_LAN)


def sign_in(local: TestClient, email: str, password: str, **form: str) -> Response:
    return local.post("/sign-in", data={"email": email, "password": password, **form})


def signed_in(local: TestClient, email: str, password: str = PASSWORD) -> dict[str, str]:
    """The headers of the browser once signed in. WebSockets need them: the client keeps the
    session cookie for https:// only, as browsers do with a Secure cookie."""
    sign_in(local, email, password)
    token = local.cookies[SESSION_COOKIE]
    local.cookies.clear()
    return {"Cookie": f"{SESSION_COOKIE}={token}"}


def test_the_first_administrator_signs_in_and_lands_on_their_dashboard(local: TestClient) -> None:
    signed_in = sign_in(local, ADMINISTRATOR, PASSWORD)

    assert signed_in.status_code == 200
    assert signed_in.url.path == "/"
    assert ADMINISTRATOR in signed_in.text
    assert local.get("/api/me").json() == {"email": ADMINISTRATOR, "role": "administrator"}


def test_the_session_cookie_is_kept_from_scripts_other_sites_and_plain_http(
    local: TestClient,
) -> None:
    signed_in = sign_in(local, ADMINISTRATOR, PASSWORD, next="/api/me")

    cookie = signed_in.history[0].headers["set-cookie"]
    name, _, attributes = cookie.partition(";")
    assert name.startswith(f"{SESSION_COOKIE}=")
    assert {attribute.strip().lower() for attribute in attributes.split(";")} >= {
        "httponly",
        "secure",
        "samesite=strict",
        "path=/",
    }


def test_the_email_is_matched_whatever_its_case(local: TestClient) -> None:
    signed_in = sign_in(local, "  First.Administrator@EXAMPLE.com ", PASSWORD, next="/api/me")

    assert signed_in.json() == {"email": ADMINISTRATOR, "role": "administrator"}


def test_a_user_who_never_set_a_password_cannot_sign_in(
    instance: Instance, local: TestClient, proxy: AccessProxy
) -> None:
    tunnel = listener_client(instance, ListenerName.TUNNEL)
    tunnel.post("/api/invitations", json={"email": USER}, headers=proxy.headers(ADMINISTRATOR))
    assert tunnel.get("/api/me", headers=proxy.headers(USER)).status_code == 200

    for password in ("", PASSWORD):
        refused = sign_in(local, USER, password)
        assert refused.status_code == 401
        assert SESSION_COOKIE not in local.cookies


@pytest.mark.parametrize(
    ("email", "password"),
    [
        (ADMINISTRATOR, "a wrong password, long enough"),
        (ADMINISTRATOR, PASSWORD.upper()),
        ("nobody@example.com", PASSWORD),
        ("not an email", PASSWORD),
    ],
    ids=["wrong-password", "wrong-case", "not-a-user", "not-an-email"],
)
def test_whatever_is_wrong_the_refusal_is_the_same(
    local: TestClient, email: str, password: str
) -> None:
    refused = sign_in(local, email, password)

    assert refused.status_code == 401
    assert "The email or the password is wrong." in refused.text
    assert SESSION_COOKIE not in local.cookies
    assert local.get("/api/me").status_code == 401


def test_the_refusal_keeps_the_email_and_speaks_italian_to_whoever_asks(local: TestClient) -> None:
    local.headers["Accept-Language"] = "it-IT,it;q=0.9"

    refused = sign_in(local, ADMINISTRATOR, "a wrong password, long enough")

    assert "L&#39;email o la password non sono corrette." in refused.text
    assert f'value="{ADMINISTRATOR}"' in refused.text


def test_an_unknown_email_is_refused_in_the_same_time_as_a_wrong_password(
    instance: Instance,
) -> None:
    """Otherwise how long the refusal takes would tell which emails are Users."""

    def fastest(email: str, address: str) -> float:
        # From an address of its own each: five failures from one address slow it down.
        client = listener_client(instance, ListenerName.LOCAL, address=address)
        times = []
        for _ in range(5):
            started = time.perf_counter()
            sign_in(client, email, "a wrong password, long enough")
            times.append(time.perf_counter() - started)
        return min(times)

    wrong_password = fastest(ADMINISTRATOR, "192.168.1.30")
    unknown_email = fastest("nobody@example.com", "192.168.1.31")

    assert 0.5 < unknown_email / wrong_password < 2


@pytest.mark.parametrize("page", ["/users", "/projects/new?repository=x&name=y"])
def test_without_a_session_a_page_sends_to_sign_in_and_back_to_it_afterwards(
    local: TestClient, page: str
) -> None:
    asked = local.get(page, follow_redirects=False)

    assert asked.status_code == 303
    sign_in_page = local.get(asked.headers["location"])
    assert sign_in_page.url.path == "/sign-in"
    assert 'type="password"' in sign_in_page.text
    back = sign_in(local, ADMINISTRATOR, PASSWORD, next=sign_in_page.url.params["next"])
    assert back.history[-1].headers["location"] == page


def test_without_a_session_the_dashboard_sends_to_sign_in(local: TestClient) -> None:
    asked = local.get("/", follow_redirects=False)

    assert (asked.status_code, asked.headers["location"]) == (303, "/sign-in")


@pytest.mark.parametrize(
    "elsewhere",
    ["https://evil.example/", "//evil.example/", "/\\evil.example/", "evil.example", ""],
)
def test_after_signing_in_only_a_page_of_this_site_is_returned_to(
    local: TestClient, elsewhere: str
) -> None:
    back = sign_in(local, ADMINISTRATOR, PASSWORD, next=elsewhere)

    assert back.history[-1].headers["location"] == "/"


def test_a_signed_in_user_opening_sign_in_goes_where_they_were_going(local: TestClient) -> None:
    sign_in(local, ADMINISTRATOR, PASSWORD)

    page = local.get("/sign-in?next=/users", follow_redirects=False)

    assert (page.status_code, page.headers["location"]) == (303, "/users")


def test_without_a_session_the_api_answers_401_naming_sign_in(local: TestClient) -> None:
    response = local.get("/api/me")

    assert response.status_code == 401
    assert response.json() == {"error": "unauthenticated", "message": "Sign in to Dockyard first."}


def test_without_a_session_a_websocket_closes_with_4401(local: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as refused, local.websocket_connect("/ws/x") as ws:
        ws.receive_text()

    assert refused.value.code == 4401


def test_the_sign_in_page_and_its_assets_need_no_session(local: TestClient) -> None:
    assert local.get("/sign-in").status_code == 200
    assert local.get("/static/terminal.js").status_code == 200
    switched = local.post("/language", data={"lang": "it", "next": "/sign-in"})
    assert "Accedi" in switched.text


def test_a_session_unused_for_seven_days_is_over(local: TestClient, clock: FakeClock) -> None:
    sign_in(local, ADMINISTRATOR, PASSWORD)

    clock.advance(days=6, hours=23)
    assert local.get("/api/me").status_code == 200
    clock.advance(days=6, hours=23)
    assert local.get("/api/me").status_code == 200
    clock.advance(days=7, seconds=1)
    assert local.get("/api/me").status_code == 401
    clock.advance(seconds=-2)  # over is over, even if the clock steps back
    assert local.get("/api/me").status_code == 401


def test_a_session_used_every_day_is_over_after_thirty_days(
    local: TestClient, clock: FakeClock
) -> None:
    sign_in(local, ADMINISTRATOR, PASSWORD)

    for _ in range(29):
        clock.advance(days=1)
        assert local.get("/api/me").status_code == 200
    clock.advance(days=1)
    assert local.get("/api/me").status_code == 401


def test_signing_out_ends_the_session(local: TestClient) -> None:
    sign_in(local, ADMINISTRATOR, PASSWORD)
    token = local.cookies[SESSION_COOKIE]

    signed_out = local.post("/sign-out")

    assert signed_out.url.path == "/sign-in"
    assert SESSION_COOKIE not in local.cookies
    local.cookies.set(SESSION_COOKIE, token)  # kept by someone
    assert local.get("/api/me").status_code == 401


def test_signing_out_ends_only_that_session(instance: Instance) -> None:
    phone, laptop = (
        listener_client(instance, ListenerName.LOCAL, address=ON_THE_LAN) for _ in range(2)
    )
    sign_in(phone, ADMINISTRATOR, PASSWORD)
    sign_in(laptop, ADMINISTRATOR, PASSWORD)

    phone.post("/sign-out")

    assert phone.get("/api/me").status_code == 401
    assert laptop.get("/api/me").status_code == 200


def test_the_page_header_offers_to_sign_out(local: TestClient) -> None:
    dashboard = sign_in(local, ADMINISTRATOR, PASSWORD)

    assert 'action="/sign-out"' in dashboard.text


def test_removing_a_user_ends_all_their_sessions(
    instance: Instance, local: TestClient, proxy: AccessProxy
) -> None:
    tunnel = listener_client(instance, ListenerName.TUNNEL)
    tunnel.post("/api/invitations", json={"email": USER}, headers=proxy.headers(ADMINISTRATOR))
    tunnel.get("/api/me", headers=proxy.headers(USER))
    instance.local_sign_in.set_password(USER, PASSWORD)
    sign_in(local, USER, PASSWORD)
    assert local.get("/api/me").json()["email"] == USER

    tunnel.delete(f"/api/users/{USER}", headers=proxy.headers(ADMINISTRATOR))

    assert local.get("/api/me").status_code == 401


def test_the_tunnel_listener_has_no_sign_in(instance: Instance, proxy: AccessProxy) -> None:
    tunnel = listener_client(instance, ListenerName.TUNNEL)
    administrator = proxy.headers(ADMINISTRATOR)

    assert tunnel.get("/sign-in", headers=administrator).status_code == 404
    form = {"email": ADMINISTRATOR, "password": PASSWORD}
    assert tunnel.post("/sign-in", data=form, headers=administrator).status_code == 404
    assert tunnel.post("/sign-out", headers=administrator).status_code == 404
    assert tunnel.post("/sign-in", data=form).status_code == 401
    assert SESSION_COOKIE not in tunnel.cookies


def test_the_tunnel_listener_never_accepts_a_local_sign_in_session(
    instance: Instance, local: TestClient
) -> None:
    session = signed_in(local, ADMINISTRATOR)
    tunnel = listener_client(instance, ListenerName.TUNNEL)

    response = tunnel.get("/api/me", headers=session)

    assert response.status_code == 401
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        tunnel.websocket_connect("/ws/x", headers=session) as ws,
    ):
        ws.receive_text()
    assert refused.value.code == 4401


ANOTHER_SITE = {"Origin": "https://another-site.example"}


def test_signing_in_from_another_site_is_refused(local: TestClient) -> None:
    """Otherwise another site could sign the browser in to an account of its choosing."""
    form = {"email": ADMINISTRATOR, "password": PASSWORD}

    refused = local.post("/sign-in", data=form, headers=ANOTHER_SITE)

    assert refused.status_code == 403
    assert SESSION_COOKIE not in local.cookies
    same_site = local.post("/sign-in", data=form, headers={"Origin": "https://testserver"})
    assert same_site.url.path == "/"


def test_a_websocket_opened_by_another_site_is_refused_even_signed_in(local: TestClient) -> None:
    session = signed_in(local, ADMINISTRATOR)

    with (
        pytest.raises(WebSocketDisconnect) as refused,
        local.websocket_connect("/ws/x", headers={**session, **ANOTHER_SITE}) as ws,
    ):
        ws.receive_text()

    assert refused.value.code == 4403


def test_projects_terminals_and_consoles_work_the_same_on_the_local_listener(
    local: TestClient, runtime: FakeRuntime
) -> None:
    runtime.publish_repository("https://github.com/octocat/Hello-World")
    session = signed_in(local, ADMINISTRATOR)

    project = running_project(local, session)

    assert "Hello" in local.get(f"/projects/{project}", headers=session).text
    with local.websocket_connect(f"/ws/projects/{project}/terminal", headers=session) as terminal:
        terminal.send_json({"type": "input", "data": "pwd\r"})
        receive_until(terminal, f"/workspace/{project}\r\n")
    with local.websocket_connect(f"/ws/projects/{project}/console", headers=session) as console:
        receive_until(console, "Remote Control ready")


def test_signing_out_of_a_session_already_over_still_forgets_it(
    local: TestClient, clock: FakeClock
) -> None:
    sign_in(local, ADMINISTRATOR, PASSWORD)
    clock.advance(days=8)

    signed_out = local.post("/sign-out")

    assert signed_out.url.path == "/sign-in"
    assert SESSION_COOKIE not in local.cookies


def test_a_websocket_without_a_session_closes_with_4401_even_where_pages_need_none(
    local: TestClient,
) -> None:
    with pytest.raises(WebSocketDisconnect) as refused, local.websocket_connect("/sign-in") as ws:
        ws.receive_text()

    assert refused.value.code == 4401
