"""Runtime contract: a User's Claude config and the login environment that connects it."""

from collections.abc import Callable

import pytest

from dockyard.runtime import LoginNotRunning, Runtime, Tty

from .conftest import MakeRuntime, Screen, wait_until


def test_a_user_who_never_signed_in_has_no_connected_claude_account(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    assert not runtime.claude_account_connected(new_user())


def test_signing_in_through_the_login_terminal_connects_the_claude_account(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    runtime.start_login_environment(user)
    assert runtime.login_environment_running(user)

    screen = Screen(runtime.attach_login_environment(user))
    screen.wait_for("https://claude.com/cai/oauth/authorize")
    screen.type("the-code-from-claude-ai\r")

    screen.wait_for("Login successful.")
    screen.wait_until_ended()
    assert runtime.claude_account_connected(user)
    assert wait_until(
        lambda: not runtime.login_environment_running(user)
    )  # the environment is gone


def test_signing_in_connects_only_that_users_claude_account(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    alice, bob = new_user(), new_user()
    runtime.start_login_environment(alice)
    runtime.start_login_environment(bob)

    sign_in(runtime, bob)

    assert runtime.claude_account_connected(bob)
    assert not runtime.claude_account_connected(alice)
    assert runtime.login_environment_running(alice)


def test_a_user_without_a_login_environment_cannot_reach_another_users(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    alice, bob = new_user(), new_user()
    runtime.start_login_environment(alice)

    assert not runtime.login_environment_running(bob)
    with pytest.raises(LoginNotRunning):
        runtime.attach_login_environment(bob)


def test_resizing_the_login_terminal_reaches_the_login_program(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    runtime.start_login_environment(user)
    screen = Screen(runtime.attach_login_environment(user))
    screen.wait_for("oauth/authorize")

    screen.tty.resize(rows=30, cols=100)
    screen.type("the-code-from-claude-ai\r")

    screen.wait_for("Terminal size: 30 100")


def test_leaving_the_login_terminal_keeps_the_login_running_and_coming_back_shows_the_link(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    runtime.start_login_environment(user)
    first = Screen(runtime.attach_login_environment(user))
    first.wait_for("oauth/authorize")

    first.tty.close()
    first.wait_until_ended()

    assert runtime.login_environment_running(user)
    again = Screen(runtime.attach_login_environment(user))
    again.wait_for("If the browser didn't open, visit: https://claude.com/cai/oauth/authorize")
    again.type("the-code-from-claude-ai\r")
    again.wait_for("Login successful.")


def test_a_failed_sign_in_leaves_the_claude_account_not_connected(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    runtime.start_login_environment(user)

    screen = sign_in(runtime, user, code="")

    assert "Login failed" in screen.text
    assert not runtime.claude_account_connected(user)
    assert wait_until(lambda: not runtime.login_environment_running(user))


def test_stopping_a_login_ends_its_environment_and_terminal(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    runtime.start_login_environment(user)
    screen = Screen(runtime.attach_login_environment(user))
    screen.wait_for("oauth/authorize")

    runtime.stop_login_environment(user)

    screen.wait_until_ended()
    assert not runtime.login_environment_running(user)
    with pytest.raises(LoginNotRunning):
        runtime.attach_login_environment(user)
    runtime.stop_login_environment(user)  # stopping again is harmless


def test_starting_a_login_again_replaces_the_running_one(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    runtime.start_login_environment(user)
    previous = Screen(runtime.attach_login_environment(user))
    previous.wait_for("oauth/authorize")

    runtime.start_login_environment(user)

    previous.wait_until_ended()
    sign_in(runtime, user)
    assert runtime.claude_account_connected(user)


def test_a_login_nobody_finishes_ends_by_itself(
    make_runtime: MakeRuntime, new_user: Callable[[], str]
) -> None:
    runtime = make_runtime(login_timeout=2)
    user = new_user()
    runtime.start_login_environment(user)
    screen = Screen(runtime.attach_login_environment(user))

    screen.wait_until_ended()

    assert wait_until(lambda: not runtime.login_environment_running(user))
    assert not runtime.claude_account_connected(user)


def test_a_user_stays_connected_after_signing_in_again(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    runtime.start_login_environment(user)
    sign_in(runtime, user)

    runtime.start_login_environment(user)
    sign_in(runtime, user, code="a-new-code")

    assert runtime.claude_account_connected(user)


def test_removing_a_claude_account_disconnects_only_that_user(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    alice, bob = new_user(), new_user()
    for user in (alice, bob):
        runtime.start_login_environment(user)
        sign_in(runtime, user)

    runtime.remove_claude_account(alice)

    assert not runtime.claude_account_connected(alice)
    assert runtime.claude_account_connected(bob)


def test_removing_a_claude_account_ends_its_login_in_progress(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()
    runtime.start_login_environment(user)
    screen = Screen(runtime.attach_login_environment(user))
    screen.wait_for("oauth/authorize")

    runtime.remove_claude_account(user)

    screen.wait_until_ended()
    assert wait_until(lambda: not runtime.login_environment_running(user))
    assert not runtime.claude_account_connected(user)


def test_removing_a_claude_account_that_was_never_connected_is_harmless(
    runtime: Runtime, new_user: Callable[[], str]
) -> None:
    user = new_user()

    runtime.remove_claude_account(user)
    runtime.remove_claude_account(user)

    assert not runtime.claude_account_connected(user)


def sign_in(runtime: Runtime, user: str, code: str = "the-code-from-claude-ai") -> Screen[Tty]:
    screen = Screen(runtime.attach_login_environment(user))
    screen.wait_for("oauth/authorize")
    screen.type(f"{code}\r")
    screen.wait_until_ended()
    return screen
