"""`python -m dockyard set-password <email>`: the installer gives a User a password from the host's
shell, the first Administrator's at installation, anyone's in an emergency."""

import os
import pty
import select
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dockyard.web import Instance, ListenerName

from .conftest import ADMINISTRATOR, AccessProxy, instance_env, listener_client
from .test_local_sign_in import LOCAL_SIGN_IN, ON_THE_LAN, PASSWORD, USER, sign_in


def set_password(
    data_dir: Path, email: str, *answers: str, **settings: str
) -> subprocess.CompletedProcess[str]:
    """Run the command with what the installer types at each question."""
    return subprocess.run(
        [sys.executable, "-m", "dockyard", "set-password", email],
        env={"PATH": os.environ["PATH"], **instance_env(data_dir), **LOCAL_SIGN_IN, **settings},
        input="".join(f"{answer}\n" for answer in answers),
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture
def local(start_instance: Callable[..., Instance]) -> Callable[[], TestClient]:
    """A fresh browser on the local listener of the Instance whose data the command changes."""

    def browser() -> TestClient:
        instance = start_instance(**LOCAL_SIGN_IN)
        return listener_client(instance, ListenerName.LOCAL, address=ON_THE_LAN)

    return browser


def can_sign_in(local: TestClient, email: str, password: str) -> bool:
    return sign_in(local, email, password).status_code == 200


def test_the_first_administrator_gets_a_password_before_dockyard_ever_started(
    tmp_path: Path, local: Callable[[], TestClient]
) -> None:
    result = set_password(tmp_path, ADMINISTRATOR, PASSWORD, PASSWORD)

    assert result.returncode == 0, result.stderr
    assert ADMINISTRATOR in result.stdout
    assert can_sign_in(local(), ADMINISTRATOR, PASSWORD)


def test_the_email_may_be_given_in_any_case(
    tmp_path: Path, local: Callable[[], TestClient]
) -> None:
    result = set_password(tmp_path, ADMINISTRATOR.upper(), PASSWORD, PASSWORD)

    assert result.returncode == 0, result.stderr
    assert can_sign_in(local(), ADMINISTRATOR, PASSWORD)


def test_it_asks_twice_and_refuses_two_different_passwords(
    tmp_path: Path, local: Callable[[], TestClient]
) -> None:
    result = set_password(tmp_path, ADMINISTRATOR, PASSWORD, PASSWORD + "!")

    assert result.returncode != 0
    assert "differ" in result.stderr
    assert not can_sign_in(local(), ADMINISTRATOR, PASSWORD)


@pytest.mark.parametrize(
    ("password", "explained"),
    [
        ("eleven char", "at least 12 characters"),
        ("", "at least 12 characters"),
        (ADMINISTRATOR.upper(), "not be the email"),
    ],
    ids=["too-short", "empty", "the-email"],
)
def test_a_weak_password_is_refused_saying_why(
    tmp_path: Path, local: Callable[[], TestClient], password: str, explained: str
) -> None:
    result = set_password(tmp_path, ADMINISTRATOR, password, password)

    assert result.returncode != 0
    assert explained in result.stderr
    assert not can_sign_in(local(), ADMINISTRATOR, password)


def test_twelve_characters_are_enough(tmp_path: Path, local: Callable[[], TestClient]) -> None:
    result = set_password(tmp_path, ADMINISTRATOR, "twelve chars", "twelve chars")

    assert result.returncode == 0, result.stderr
    assert can_sign_in(local(), ADMINISTRATOR, "twelve chars")


def test_it_works_for_any_user_as_an_emergency_reset(
    tmp_path: Path,
    start_instance: Callable[..., Instance],
    proxy: AccessProxy,
    local: Callable[[], TestClient],
) -> None:
    tunnel = listener_client(start_instance(**LOCAL_SIGN_IN), ListenerName.TUNNEL)
    tunnel.post("/api/invitations", json={"email": USER}, headers=proxy.headers(ADMINISTRATOR))
    tunnel.get("/api/me", headers=proxy.headers(USER))

    result = set_password(tmp_path, USER, PASSWORD, PASSWORD)

    assert result.returncode == 0, result.stderr
    assert can_sign_in(local(), USER, PASSWORD)


def test_it_signs_the_user_out_everywhere(tmp_path: Path, local: Callable[[], TestClient]) -> None:
    set_password(tmp_path, ADMINISTRATOR, PASSWORD, PASSWORD)
    browser = local()
    sign_in(browser, ADMINISTRATOR, PASSWORD)
    assert browser.get("/api/me").status_code == 200

    set_password(tmp_path, ADMINISTRATOR, "a brand new password", "a brand new password")

    assert browser.get("/api/me").status_code == 401


@pytest.mark.parametrize("invited", [False, True], ids=["stranger", "invited-not-yet-a-user"])
def test_an_email_that_is_not_a_user_is_refused(
    tmp_path: Path,
    start_instance: Callable[..., Instance],
    proxy: AccessProxy,
    local: Callable[[], TestClient],
    invited: bool,
) -> None:
    """Users are made by Invitations, never by this command."""
    if invited:
        tunnel = listener_client(start_instance(**LOCAL_SIGN_IN), ListenerName.TUNNEL)
        tunnel.post("/api/invitations", json={"email": USER}, headers=proxy.headers(ADMINISTRATOR))

    result = set_password(tmp_path, USER, PASSWORD, PASSWORD)

    assert result.returncode != 0
    assert "neither a User nor the first Administrator" in result.stderr
    assert not can_sign_in(local(), USER, PASSWORD)


def test_with_local_sign_in_off_it_sets_the_password_and_warns(tmp_path: Path) -> None:
    result = set_password(tmp_path, ADMINISTRATOR, PASSWORD, PASSWORD, DOCKYARD_LOCAL_PORT="")

    assert result.returncode == 0, result.stderr
    assert "Local sign-in is off" in result.stderr


def test_with_an_invalid_configuration_it_refuses_like_dockyard(tmp_path: Path) -> None:
    result = set_password(
        tmp_path, ADMINISTRATOR, PASSWORD, PASSWORD, DOCKYARD_FIRST_ADMINISTRATOR_EMAIL=""
    )

    assert result.returncode == 2
    assert "DOCKYARD_FIRST_ADMINISTRATOR_EMAIL" in result.stderr


def test_an_unknown_command_is_refused(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "dockyard", "make-me-administrator"],
        env={"PATH": os.environ["PATH"], **instance_env(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert "set-password" in result.stderr


def test_the_password_typed_at_a_terminal_is_not_shown(tmp_path: Path) -> None:
    """As with `docker compose exec`, which gives the command a terminal."""
    env = {"PATH": os.environ["PATH"], **instance_env(tmp_path), **LOCAL_SIGN_IN}
    controller, terminal = pty.openpty()
    command = subprocess.Popen(
        [sys.executable, "-m", "dockyard", "set-password", ADMINISTRATOR],
        env=env,
        stdin=terminal,
        stdout=terminal,
        stderr=terminal,
        start_new_session=True,  # the pty is the only terminal it can ask on
    )
    os.close(terminal)
    shown = ""
    try:
        for _ in range(2):
            shown += _read_until(controller, "assword")
            os.write(controller, f"{PASSWORD}\n".encode())
        shown += _read_until(controller, ADMINISTRATOR)
        assert command.wait(timeout=30) == 0
    finally:
        command.kill()
        os.close(controller)

    assert PASSWORD not in shown


def _read_until(fd: int, text: str, timeout: float = 20) -> str:
    shown = ""
    deadline = time.monotonic() + timeout
    while text not in shown:
        assert time.monotonic() < deadline, shown
        if select.select([fd], [], [], 0.1)[0]:
            read = os.read(fd, 1024)
            assert read, shown  # the command closed the terminal
            shown += read.decode(errors="replace")
    return shown
