"""`python -m dockyard remove-second-factor <email>`: the installer removes a User's Second factor
from the host's shell, for an Administrator who lost their phone and their recovery codes and so
cannot reach the Users page."""

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dockyard.web import Instance, ListenerName

from .conftest import ADMINISTRATOR, FakeClock, instance_env, listener_client
from .test_local_sign_in import LOCAL_SIGN_IN, ON_THE_LAN, PASSWORD, USER, sign_in
from .test_second_factor import turn_on


def remove_second_factor(
    data_dir: Path, email: str, **settings: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "dockyard", "remove-second-factor", email],
        env={"PATH": os.environ["PATH"], **instance_env(data_dir), **LOCAL_SIGN_IN, **settings},
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture
def instance(start_instance: Callable[..., Instance]) -> Instance:
    started = start_instance(**LOCAL_SIGN_IN)
    started.local_sign_in.set_password(ADMINISTRATOR, PASSWORD)
    return started


@pytest.fixture
def locked_out(instance: Instance, clock: FakeClock) -> None:
    """The first Administrator has a Second factor, and neither their phone nor their codes."""
    browser = listener_client(instance, ListenerName.LOCAL, address=ON_THE_LAN)
    sign_in(browser, ADMINISTRATOR, PASSWORD)
    turn_on(browser, clock)


def browser(instance: Instance) -> TestClient:
    return listener_client(instance, ListenerName.LOCAL, address=ON_THE_LAN)


@pytest.mark.usefixtures("locked_out")
def test_the_administrator_signs_in_with_the_password_alone_afterwards(
    tmp_path: Path, instance: Instance
) -> None:
    assert sign_in(browser(instance), ADMINISTRATOR, PASSWORD).url.path == "/sign-in/code"

    result = remove_second_factor(tmp_path, ADMINISTRATOR)

    assert result.returncode == 0, result.stderr
    assert f"Second factor of {ADMINISTRATOR} removed" in result.stdout
    assert sign_in(browser(instance), ADMINISTRATOR, PASSWORD).url.path == "/"


@pytest.mark.usefixtures("locked_out")
def test_the_email_may_be_given_in_any_case(tmp_path: Path, instance: Instance) -> None:
    result = remove_second_factor(tmp_path, ADMINISTRATOR.upper())

    assert result.returncode == 0, result.stderr
    assert not instance.local_sign_in.has_second_factor(ADMINISTRATOR)


@pytest.mark.usefixtures("instance")
def test_an_email_that_is_not_a_user_is_refused(tmp_path: Path) -> None:
    result = remove_second_factor(tmp_path, USER)

    assert result.returncode != 0
    assert f"{USER} is not a User of this Instance: nothing was changed." in result.stderr


@pytest.mark.usefixtures("instance")
def test_a_user_without_a_second_factor_is_refused(tmp_path: Path) -> None:
    result = remove_second_factor(tmp_path, ADMINISTRATOR)

    assert result.returncode != 0
    assert f"{ADMINISTRATOR} has no Second factor: nothing was changed." in result.stderr


@pytest.mark.usefixtures("locked_out")
def test_with_local_sign_in_off_it_removes_it_and_warns(tmp_path: Path, instance: Instance) -> None:
    result = remove_second_factor(tmp_path, ADMINISTRATOR, DOCKYARD_LOCAL_PORT="")

    assert result.returncode == 0, result.stderr
    assert "Local sign-in is off" in result.stderr
    assert not instance.local_sign_in.has_second_factor(ADMINISTRATOR)


def test_with_an_invalid_configuration_it_refuses_like_dockyard(tmp_path: Path) -> None:
    result = remove_second_factor(tmp_path, ADMINISTRATOR, DOCKYARD_FIRST_ADMINISTRATOR_EMAIL="")

    assert result.returncode == 2
    assert "DOCKYARD_FIRST_ADMINISTRATOR_EMAIL" in result.stderr
