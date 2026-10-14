"""Who gets in: identity comes only from the authentication proxy, access only to Users."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from .conftest import ADMINISTRATOR, AccessProxy, new_signing_key

STRANGER = "stranger@example.com"


def test_first_administrator_sees_the_dashboard_with_their_email_and_role(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    page = dockyard.get("/", headers=proxy.headers(ADMINISTRATOR))

    assert page.status_code == 200
    assert ADMINISTRATOR in page.text
    assert "Administrator" in page.text


def test_api_tells_the_first_administrator_who_they_are(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    me = dockyard.get("/api/me", headers=proxy.headers(ADMINISTRATOR))

    assert me.status_code == 200
    assert me.json() == {"email": ADMINISTRATOR, "role": "administrator"}


def test_the_proxy_cookie_is_accepted_like_the_header(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    dockyard.cookies.set("CF_Authorization", proxy.token(ADMINISTRATOR))

    assert dockyard.get("/api/me").json()["email"] == ADMINISTRATOR


def test_emails_are_matched_regardless_of_case(dockyard: TestClient, proxy: AccessProxy) -> None:
    me = dockyard.get("/api/me", headers=proxy.headers("First.Administrator@Example.COM"))

    assert me.json() == {"email": ADMINISTRATOR, "role": "administrator"}


@pytest.mark.parametrize("path", ["/", "/api/me", "/no/such/page"])
def test_requests_without_a_proxy_identity_are_refused(dockyard: TestClient, path: str) -> None:
    assert dockyard.get(path).status_code == 401


def test_the_refusal_page_explains_that_the_proxy_is_required(dockyard: TestClient) -> None:
    page = dockyard.get("/")

    assert "authentication proxy" in page.text
    assert "Projects" not in page.text


@pytest.mark.parametrize(
    "token_for",
    [
        pytest.param(
            lambda p: p.token(ADMINISTRATOR, key=new_signing_key()), id="signed-by-someone-else"
        ),
        pytest.param(
            lambda p: p.token(ADMINISTRATOR, audience="other-app"), id="another-application"
        ),
        pytest.param(
            lambda p: p.token(ADMINISTRATOR, issuer="https://evil.cloudflareaccess.com"),
            id="another-team",
        ),
        pytest.param(lambda p: p.token(ADMINISTRATOR, expires_in=-60), id="expired"),
        pytest.param(lambda p: p.token(None), id="no-email"),
        pytest.param(lambda p: "not-a-token", id="garbage"),
    ],
)
def test_tokens_the_proxy_did_not_issue_for_this_instance_are_refused(
    dockyard: TestClient, proxy: AccessProxy, token_for: Callable[[AccessProxy], str]
) -> None:
    response = dockyard.get("/api/me", headers={"Cf-Access-Jwt-Assertion": token_for(proxy)})

    assert response.status_code == 401
    assert response.json()["error"] == "unauthenticated"


def test_a_verified_email_that_is_not_a_user_gets_the_not_invited_page(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    page = dockyard.get("/", headers=proxy.headers(STRANGER))

    assert page.status_code == 403
    assert "Not invited" in page.text
    assert STRANGER in page.text
    assert "Projects" not in page.text


def test_a_verified_email_that_is_not_a_user_is_refused_by_the_api(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    response = dockyard.get("/api/me", headers=proxy.headers(STRANGER))

    assert response.status_code == 403
    assert response.json()["error"] == "not_invited"


def test_websockets_without_a_proxy_identity_are_refused(dockyard: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as refused, dockyard.websocket_connect("/ws/x") as ws:
        ws.receive_text()

    assert refused.value.code == 4401


def test_websockets_of_a_verified_email_that_is_not_a_user_are_refused(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        dockyard.websocket_connect("/ws/x", headers=proxy.headers(STRANGER)) as ws,
    ):
        ws.receive_text()

    assert refused.value.code == 4403


def test_websockets_of_a_user_get_past_the_identity_check(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        dockyard.websocket_connect("/ws/anything", headers=proxy.headers(ADMINISTRATOR)),
    ):
        pass

    assert closed.value.code not in (4401, 4403)  # no such socket yet, but not an identity refusal


@pytest.mark.parametrize(
    "setting", [{"DOCKYARD_CF_ACCESS_TEAM": "other-team"}, {"DOCKYARD_CF_ACCESS_AUD": "other-app"}]
)
def test_the_proxy_settings_decide_which_tokens_are_trusted(
    start_dockyard: Callable[..., TestClient], proxy: AccessProxy, setting: dict[str, str]
) -> None:
    dockyard = start_dockyard(**setting)

    response = dockyard.get(
        "/api/me", headers=proxy.headers(ADMINISTRATOR)
    )  # issued for TEAM, AUDIENCE

    assert response.status_code == 401


def test_people_are_told_when_their_identity_cannot_be_verified_right_now(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    with proxy.signing_keys_unreachable():
        page = dockyard.get("/", headers=proxy.headers(ADMINISTRATOR))

    assert page.status_code == 503
    assert "cannot verify who you are" in page.text


def test_the_api_reports_when_identities_cannot_be_verified_right_now(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    with proxy.signing_keys_unreachable():
        response = dockyard.get("/api/me", headers=proxy.headers(ADMINISTRATOR))

    assert response.status_code == 503
    assert response.json()["error"] == "identity_unavailable"


def test_a_signed_in_user_asking_for_a_missing_page_gets_a_page_in_their_language(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    page = dockyard.get(
        "/no/such/page", headers={**proxy.headers(ADMINISTRATOR), "Accept-Language": "it"}
    )

    assert page.status_code == 404
    assert '<html lang="it">' in page.text
    assert "Pagina non trovata" in page.text
