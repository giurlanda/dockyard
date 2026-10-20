"""The Second factor: a User turns it on from their own page with an authenticator app, and from
then on Local sign-in asks for a code after the password. Recovery codes stand in for the app,
once each. Signing in through Cloudflare never asks for it.

The codes are computed here from the secret the enrolment page shows, as an authenticator app
does (RFC 6238: SHA-1, 6 digits, 30-second steps), and the clock is moved on for the steps."""

import base64
import hashlib
import hmac
import re
import sqlite3
import struct
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dockyard.web import Instance, ListenerName
from dockyard.web.local_sign_in import CODE_STEP_COOKIE

from .conftest import ADMINISTRATOR, AccessProxy, FakeClock, listener_client
from .test_local_sign_in import LOCAL_SIGN_IN, ON_THE_LAN, PASSWORD, sign_in

STEP = timedelta(seconds=30)


def authenticator_code(secret: str, at: datetime | float) -> str:
    """The code an authenticator app shows at that time for the secret, as typed or scanned."""
    key = secret.replace(" ", "").upper()
    key = base64.b32decode(key + "=" * (-len(key) % 8))
    seconds = at.timestamp() if isinstance(at, datetime) else at
    digest = hmac.new(key, struct.pack(">Q", int(seconds) // 30), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    (number,) = struct.unpack(">I", digest[offset : offset + 4])
    return f"{(number & 0x7FFFFFFF) % 1_000_000:06d}"


def another(code: str) -> str:
    """A code that is not this one."""
    return f"{(int(code) + 1) % 1_000_000:06d}"


def test_the_codes_computed_here_are_those_of_rfc_6238() -> None:
    secret = base64.b32encode(b"12345678901234567890").decode()

    assert authenticator_code(secret, 59) == "287082"
    assert authenticator_code(secret, 1111111109) == "081804"
    assert authenticator_code(secret, 2000000000) == "279037"


@pytest.fixture
def instance(start_instance: Callable[..., Instance]) -> Instance:
    started = start_instance(**LOCAL_SIGN_IN)
    started.local_sign_in.set_password(ADMINISTRATOR, PASSWORD)
    return started


@pytest.fixture
def local(instance: Instance) -> TestClient:
    """The first Administrator, signed in on the local listener."""
    client = listener_client(instance, ListenerName.LOCAL, address=ON_THE_LAN)
    assert sign_in(client, ADMINISTRATOR, PASSWORD).url.path == "/"
    return client


def secret_on(page: str) -> str:
    found = re.search(r'id="second-factor-secret"[^>]*>([A-Z2-7 ]+)<', page)
    assert found, page
    return found.group(1)


def recovery_codes_on(page: str) -> list[str]:
    return re.findall(r'<li class="recovery-code"><code>([a-z0-9-]+)</code></li>', page)


def start_turning_on(local: TestClient) -> str:
    """The secret the enrolment page shows."""
    page = turn_on_with(local, PASSWORD)
    assert page.status_code == 200, page.text
    return secret_on(page.text)


def turn_on_with(local: TestClient, password: str) -> Any:
    return local.post("/me/second-factor", data={"password": password})


def new_recovery_codes(local: TestClient, code_or_password: str) -> Any:
    return local.post(
        "/me/second-factor/recovery-codes", data={"code_or_password": code_or_password}
    )


def confirm(local: TestClient, code: str) -> Any:
    return local.post("/me/second-factor/confirm", data={"code": code})


def turn_on(local: TestClient, clock: FakeClock) -> tuple[str, list[str]]:
    """The secret and the recovery codes. The clock moves on a step: the code used to confirm
    is not used twice."""
    secret = start_turning_on(local)
    confirmed = confirm(local, authenticator_code(secret, clock.now()))
    assert confirmed.status_code == 200, confirmed.text
    clock.advance(seconds=30)
    return secret, recovery_codes_on(confirmed.text)


def give_code(local: TestClient, code: str, **form: str) -> Any:
    return local.post("/sign-in/code", data={"code": code, **form})


def sign_in_again(local: TestClient, password: str = PASSWORD, **form: str) -> Any:
    local.post("/sign-out")
    return sign_in(local, ADMINISTRATOR, password, **form)


def test_turning_it_on_shows_a_qr_code_and_the_secret_and_takes_effect_after_a_correct_code(
    local: TestClient, clock: FakeClock
) -> None:
    page = turn_on_with(local, PASSWORD)

    assert page.status_code == 200
    assert "<svg" in page.text
    secret = secret_on(page.text)
    assert len(secret.replace(" ", "")) == 32
    assert page.headers["cache-control"] == "no-store"
    assert local.get("/me/second-factor").text.count(secret) == 1  # shown again until confirmed
    assert sign_in_again(local).url.path == "/"  # not yet: no code asked for

    wrong = confirm(local, another(authenticator_code(secret, clock.now())))
    assert wrong.status_code == 422
    assert "The code is wrong" in wrong.text
    assert secret_on(wrong.text) == secret
    assert sign_in_again(local).url.path == "/"

    confirmed = confirm(local, authenticator_code(secret, clock.now()))

    assert confirmed.status_code == 200
    assert confirmed.headers["cache-control"] == "no-store"
    codes = recovery_codes_on(confirmed.text)
    assert len(set(codes)) == 10
    assert "You have a Second factor" in local.get("/me").text
    assert local.get("/me/second-factor").url.path == "/me"
    clock.advance(seconds=30)
    asked = sign_in_again(local)
    assert asked.url.path == "/sign-in/code"
    assert local.get("/api/me").status_code == 401


def test_the_recovery_codes_are_shown_once(local: TestClient, clock: FakeClock) -> None:
    _, codes = turn_on(local, clock)

    page = local.get("/me").text

    assert "10 recovery codes left" in page
    assert not any(code in page for code in codes)


def test_the_users_page_says_whether_they_have_a_second_factor(
    local: TestClient, clock: FakeClock
) -> None:
    assert "You have no Second factor" in local.get("/me").text

    turn_on(local, clock)

    assert "You have a Second factor" in local.get("/me").text


def test_with_a_second_factor_a_code_after_the_password_signs_in(
    local: TestClient, clock: FakeClock
) -> None:
    secret, _ = turn_on(local, clock)

    asked = sign_in_again(local, next="/api/me")
    assert asked.status_code == 200
    assert asked.url.path == "/sign-in/code"
    assert "authenticator app" in asked.text
    assert 'name="next" value="/api/me"' in asked.text
    signed_in = give_code(local, authenticator_code(secret, clock.now()), next="/api/me")

    assert signed_in.status_code == 200
    assert signed_in.json() == {"email": ADMINISTRATOR, "role": "administrator"}


def test_a_wrong_password_never_reaches_the_code_step(local: TestClient, clock: FakeClock) -> None:
    turn_on(local, clock)

    refused = sign_in_again(local, "a wrong password, long enough")

    assert refused.status_code == 401
    assert "The email or the password is wrong." in refused.text


def test_a_wrong_code_is_refused_and_the_step_goes_on(local: TestClient, clock: FakeClock) -> None:
    secret, _ = turn_on(local, clock)
    sign_in_again(local)
    right = authenticator_code(secret, clock.now())

    refused = give_code(local, another(right))

    assert refused.status_code == 401
    assert "The code is wrong." in refused.text
    assert local.get("/api/me").status_code == 401
    assert give_code(local, right).url.path == "/"


def test_the_code_step_expires_after_five_minutes(local: TestClient, clock: FakeClock) -> None:
    secret, _ = turn_on(local, clock)
    sign_in_again(local)

    clock.advance(minutes=5)
    expired = give_code(local, authenticator_code(secret, clock.now()))

    assert expired.status_code == 401
    assert expired.url.path == "/sign-in/code"
    assert "Sign in again" in expired.text
    assert local.get("/api/me").status_code == 401
    assert local.get("/sign-in/code").url.path == "/sign-in"


def test_the_code_step_works_once(local: TestClient, clock: FakeClock) -> None:
    secret, codes = turn_on(local, clock)
    sign_in_again(local)
    step = local.cookies[CODE_STEP_COOKIE]
    give_code(local, authenticator_code(secret, clock.now()))
    local.post("/sign-out")
    local.cookies.clear()

    clock.advance(seconds=30)
    again = local.post(
        "/sign-in/code",
        data={"code": codes[0]},
        headers={"Cookie": f"{CODE_STEP_COOKIE}={step}"},
    )

    assert again.status_code == 401
    assert "Sign in again" in again.text
    assert local.get("/api/me").status_code == 401


def test_a_code_one_step_early_or_late_is_accepted_but_not_two(
    local: TestClient, clock: FakeClock
) -> None:
    secret, _ = turn_on(local, clock)
    clock.advance(minutes=10)

    for offset, accepted in [(-2, False), (2, False), (-1, True), (1, True)]:
        sign_in_again(local)
        given = give_code(local, authenticator_code(secret, clock.now() + offset * STEP))
        assert (given.url.path == "/") is accepted, offset
        clock.advance(minutes=1)


def test_a_code_already_used_in_the_same_or_an_earlier_step_is_refused(
    local: TestClient, clock: FakeClock
) -> None:
    secret, _ = turn_on(local, clock)
    sign_in_again(local)
    used = authenticator_code(secret, clock.now())
    earlier = authenticator_code(secret, clock.now() - STEP)
    assert give_code(local, used).url.path == "/"

    sign_in_again(local)
    assert give_code(local, used).status_code == 401
    assert give_code(local, earlier).status_code == 401
    assert give_code(local, authenticator_code(secret, clock.now() + STEP)).url.path == "/"


def test_a_recovery_code_stands_in_for_the_app_once(local: TestClient, clock: FakeClock) -> None:
    _, codes = turn_on(local, clock)

    sign_in_again(local)
    assert give_code(local, codes[0].upper().replace("-", " ")).url.path == "/"
    assert "9 recovery codes left" in local.get("/me").text

    sign_in_again(local)
    assert give_code(local, codes[0]).status_code == 401
    assert give_code(local, codes[1]).url.path == "/"


def test_new_recovery_codes_void_the_old_ones(local: TestClient, clock: FakeClock) -> None:
    _, old = turn_on(local, clock)

    made = new_recovery_codes(local, PASSWORD)

    assert made.status_code == 200
    assert made.headers["cache-control"] == "no-store"
    new = recovery_codes_on(made.text)
    assert len(set(new)) == 10
    assert not set(new) & set(old)
    sign_in_again(local)
    assert give_code(local, old[2]).status_code == 401
    assert give_code(local, new[2]).url.path == "/"


def test_new_recovery_codes_need_a_current_code_or_the_password(
    local: TestClient, clock: FakeClock
) -> None:
    secret, old = turn_on(local, clock)

    refused = new_recovery_codes(local, "a wrong password, long enough")

    assert refused.status_code == 403
    assert "The code or the password is wrong." in refused.text
    assert not recovery_codes_on(refused.text)
    made = new_recovery_codes(local, authenticator_code(secret, clock.now()))
    assert len(recovery_codes_on(made.text)) == 10
    sign_in_again(local)
    assert give_code(local, old[0]).status_code == 401


def test_turning_it_on_needs_the_password(local: TestClient, clock: FakeClock) -> None:
    for _ in range(5):
        refused = turn_on_with(local, "a wrong password, long enough")
        assert refused.status_code == 403
        assert "The password is wrong." in refused.text
        assert "second-factor-secret" not in refused.text

    slowed_down = turn_on_with(local, PASSWORD)

    assert slowed_down.status_code == 429
    assert "Too many failed attempts. Try again in 30 s." in slowed_down.text
    assert local.get("/me/second-factor").url.path == "/me"  # nothing waits for a code
    clock.advance(seconds=30)
    assert "second-factor-secret" in turn_on_with(local, PASSWORD).text


def test_wrong_codes_slow_down_like_wrong_passwords(
    instance: Instance, local: TestClient, clock: FakeClock
) -> None:
    secret, _ = turn_on(local, clock)
    sign_in_again(local)
    right = authenticator_code(secret, clock.now())
    wrong = another(right)
    for _ in range(5):
        assert give_code(local, wrong).status_code == 401

    slowed_down = give_code(local, right)

    assert slowed_down.status_code == 429
    assert slowed_down.headers["retry-after"] == "30"
    assert "Too many failed attempts. Try again in 30 s." in slowed_down.text
    assert local.get("/api/me").status_code == 401
    clock.advance(seconds=30)
    # The right password, even from elsewhere, does not forget the email's wrong codes.
    elsewhere = listener_client(instance, ListenerName.LOCAL, address="10.8.0.7")
    assert sign_in(elsewhere, ADMINISTRATOR, PASSWORD).url.path == "/sign-in/code"
    assert give_code(elsewhere, wrong).status_code == 401
    assert give_code(elsewhere, authenticator_code(secret, clock.now())).status_code == 429


def turn_off(local: TestClient, code_or_password: str) -> Any:
    return local.post("/me/second-factor/off", data={"code_or_password": code_or_password})


def test_turning_it_off_needs_a_current_code(local: TestClient, clock: FakeClock) -> None:
    secret, _ = turn_on(local, clock)

    refused = turn_off(local, "a wrong password, long enough")
    assert refused.status_code == 403
    assert "The code or the password is wrong." in refused.text
    assert "You have a Second factor" in refused.text

    turned_off = turn_off(local, authenticator_code(secret, clock.now()))

    assert turned_off.status_code == 200
    assert turned_off.url.path == "/me"
    assert "You have no Second factor" in turned_off.text
    assert sign_in_again(local).url.path == "/"


def test_turning_it_off_takes_the_password_too(local: TestClient, clock: FakeClock) -> None:
    _, codes = turn_on(local, clock)

    assert turn_off(local, PASSWORD).url.path == "/me"

    assert sign_in_again(local).url.path == "/"
    assert "You have no Second factor" in local.get("/me").text
    turn_on(local, clock)  # again, with new recovery codes
    sign_in_again(local)
    assert give_code(local, codes[0]).status_code == 401


def test_wrong_attempts_at_turning_it_off_slow_down(local: TestClient, clock: FakeClock) -> None:
    turn_on(local, clock)
    for _ in range(5):
        assert turn_off(local, "a wrong password, long enough").status_code == 403

    slowed_down = turn_off(local, PASSWORD)

    assert slowed_down.status_code == 429
    assert "You have a Second factor" in slowed_down.text


def test_the_secret_is_stored_encrypted(
    instance: Instance, local: TestClient, clock: FakeClock, tmp_path: Path
) -> None:
    secret = start_turning_on(local)
    raw = base64.b32decode(secret.replace(" ", ""))
    confirm(local, authenticator_code(secret, clock.now()))

    with sqlite3.connect(tmp_path / "dockyard.db") as conn:
        dump = "\n".join(conn.iterdump())

    assert secret.replace(" ", "") not in dump
    assert raw.hex() not in dump.lower()


def test_signing_in_through_cloudflare_never_asks_for_it(
    instance: Instance, local: TestClient, clock: FakeClock, proxy: AccessProxy
) -> None:
    turn_on(local, clock)
    tunnel = listener_client(instance, ListenerName.TUNNEL)

    assert tunnel.get("/api/me", headers=proxy.headers(ADMINISTRATOR)).status_code == 200
    page = tunnel.get("/me", headers=proxy.headers(ADMINISTRATOR))
    assert "You have a Second factor" in page.text
    assert "open this page on the local address" in page.text


def test_the_tunnel_listener_never_manages_it(
    instance: Instance, proxy: AccessProxy, clock: FakeClock
) -> None:
    tunnel = listener_client(instance, ListenerName.TUNNEL)
    tunnel.headers.update(proxy.headers(ADMINISTRATOR))

    for path in ["", "/confirm", "/recovery-codes", "/off"]:
        assert tunnel.post(f"/me/second-factor{path}", data={"code": "1"}).status_code == 404
    assert tunnel.post("/sign-in/code", data={"code": "123456"}).status_code == 404


def test_without_local_sign_in_there_is_no_second_factor(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    dockyard.headers.update(proxy.headers(ADMINISTRATOR))

    assert "Second factor" not in dockyard.get("/me").text
    assert dockyard.post("/me/second-factor").status_code == 404


def test_the_second_factor_speaks_italian(local: TestClient, clock: FakeClock) -> None:
    local.headers["Accept-Language"] = "it"
    assert "Non hai un Secondo fattore" in local.get("/me").text

    enrolment = turn_on_with(local, PASSWORD)
    assert "app di autenticazione" in enrolment.text
    secret = secret_on(enrolment.text)
    confirmed = confirm(local, authenticator_code(secret, clock.now()))
    assert "codici di recupero" in confirmed.text
    clock.advance(seconds=30)

    asked = sign_in_again(local)
    assert "Codice" in asked.text
    assert "Il codice è sbagliato." in give_code(local, "1").text
    assert give_code(local, authenticator_code(secret, clock.now())).url.path == "/"
    assert "Hai un Secondo fattore" in local.get("/me").text
