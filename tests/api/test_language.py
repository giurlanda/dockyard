"""The UI speaks English or Italian."""

import pytest
from fastapi.testclient import TestClient

from .conftest import ADMINISTRATOR, AccessProxy

ITALIAN = {"Accept-Language": "it-IT,it;q=0.9,en;q=0.5"}


def test_the_dashboard_is_in_english_by_default(dockyard: TestClient, proxy: AccessProxy) -> None:
    page = dockyard.get("/", headers=proxy.headers(ADMINISTRATOR))

    assert '<html lang="en">' in page.text
    assert "Projects" in page.text


def test_the_dashboard_follows_an_italian_browser(dockyard: TestClient, proxy: AccessProxy) -> None:
    page = dockyard.get("/", headers={**proxy.headers(ADMINISTRATOR), **ITALIAN})

    assert '<html lang="it">' in page.text
    assert "Progetti" in page.text
    assert "Amministratore" in page.text


def test_a_browser_language_dockyard_does_not_speak_falls_back_to_english(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    page = dockyard.get(
        "/", headers={**proxy.headers(ADMINISTRATOR), "Accept-Language": "fr-FR,de;q=0.8"}
    )

    assert '<html lang="en">' in page.text


def test_switching_to_italian_sticks_across_pages(dockyard: TestClient, proxy: AccessProxy) -> None:
    switched = dockyard.post("/language", data={"lang": "it"}, headers=proxy.headers(ADMINISTRATOR))
    assert switched.status_code == 200  # redirected back to the dashboard

    page = dockyard.get("/", headers=proxy.headers(ADMINISTRATOR))

    assert "Progetti" in page.text


def test_switching_back_to_english_overrides_an_italian_browser(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    dockyard.post("/language", data={"lang": "en"}, headers=proxy.headers(ADMINISTRATOR))

    page = dockyard.get("/", headers={**proxy.headers(ADMINISTRATOR), **ITALIAN})

    assert "Projects" in page.text


def test_switching_returns_to_the_page_the_user_was_on(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    switched = dockyard.post(
        "/language",
        data={"lang": "it", "next": "/"},
        headers=proxy.headers(ADMINISTRATOR),
        follow_redirects=False,
    )

    assert switched.status_code == 303
    assert switched.headers["location"] == "/"


@pytest.mark.parametrize("elsewhere", ["https://evil.example/", "//evil.example/", "javascript:x"])
def test_switching_never_redirects_to_another_site(
    dockyard: TestClient, proxy: AccessProxy, elsewhere: str
) -> None:
    switched = dockyard.post(
        "/language",
        data={"lang": "it", "next": elsewhere},
        headers=proxy.headers(ADMINISTRATOR),
        follow_redirects=False,
    )

    assert switched.headers["location"] == "/"


def test_a_language_dockyard_does_not_speak_is_refused(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    response = dockyard.post("/language", data={"lang": "fr"}, headers=proxy.headers(ADMINISTRATOR))

    assert response.status_code == 422


def test_refusal_pages_are_localised_too(dockyard: TestClient, proxy: AccessProxy) -> None:
    not_invited = dockyard.get("/", headers={**proxy.headers("stranger@example.com"), **ITALIAN})
    unauthenticated = dockyard.get("/", headers=ITALIAN)

    assert "Invito mancante" in not_invited.text
    assert "proxy di autenticazione" in unauthenticated.text
