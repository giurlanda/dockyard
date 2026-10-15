"""A User keeps Git Credentials in their profile: adds, renames, rotates and deletes them."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from .conftest import ADMINISTRATOR, AccessProxy

OTHER_USER = "another.user@example.com"
TOKEN = "github_pat_11AAAAAAA0the0first0token0ever0saved"


def add(dockyard: TestClient, headers: dict[str, str], **fields: str) -> Any:
    body = {"label": "Work account", "provider": "github", "token": TOKEN, **fields}
    return dockyard.post("/api/git-credentials", json=body, headers=headers)


def test_a_user_adds_a_git_credential_and_sees_it_listed_without_its_token(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)

    added = add(dockyard, signed_in, label="Work account", provider="github")

    assert added.status_code == 201
    credential = added.json()
    assert (credential["label"], credential["provider"]) == ("Work account", "github")
    assert TOKEN not in added.text
    listed = dockyard.get("/api/git-credentials", headers=signed_in)
    assert [(c["id"], c["label"], c["provider"]) for c in listed.json()] == [
        (credential["id"], "Work account", "github")
    ]
    assert TOKEN not in listed.text


def test_a_user_holds_several_git_credentials_for_the_same_provider(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)

    add(dockyard, signed_in, label="Work", provider="gitlab", token="glpat-work0token")
    add(dockyard, signed_in, label="Personal", provider="gitlab", token="glpat-personal0token")
    add(dockyard, signed_in, label="Open source", provider="github")

    listed = dockyard.get("/api/git-credentials", headers=signed_in).json()
    assert [(c["label"], c["provider"]) for c in listed] == [
        ("Work", "gitlab"),
        ("Personal", "gitlab"),
        ("Open source", "github"),
    ]


def test_a_copy_of_the_instance_data_alone_does_not_reveal_a_token(
    dockyard: TestClient, proxy: AccessProxy, tmp_path: Path
) -> None:
    add(dockyard, proxy.headers(ADMINISTRATOR))

    for stored in tmp_path.rglob("*"):
        if stored.is_file():
            assert TOKEN.encode() not in stored.read_bytes(), stored


@pytest.mark.parametrize(
    ("fields", "error", "message"),
    [
        ({"label": "  "}, "label_missing", "label"),
        ({"label": "x" * 61}, "label_invalid", "60 characters"),
        ({"label": "two\nlines"}, "label_invalid", "one line"),
        ({"token": ""}, "token_missing", "Paste the token"),
        ({"token": "ghp_has a space"}, "token_invalid", "exactly as"),
        ({"token": "ghp_x\n  user: someone"}, "token_invalid", "exactly as"),
    ],
)
def test_a_git_credential_without_a_proper_label_or_token_is_refused_with_a_reason(
    dockyard: TestClient,
    proxy: AccessProxy,
    fields: dict[str, str],
    error: str,
    message: str,
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)

    refused = add(dockyard, signed_in, **fields)

    assert refused.status_code == 422
    assert refused.json()["error"] == error
    assert message in refused.json()["message"]
    assert dockyard.get("/api/git-credentials", headers=signed_in).json() == []


def test_only_github_and_gitlab_are_accepted_as_providers(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    refused = add(dockyard, proxy.headers(ADMINISTRATOR), provider="bitbucket")

    assert refused.status_code == 422


def test_a_user_renames_a_git_credential(dockyard: TestClient, proxy: AccessProxy) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    credential = add(dockyard, signed_in, label="Work").json()

    renamed = dockyard.patch(
        f"/api/git-credentials/{credential['id']}", json={"label": "Acme"}, headers=signed_in
    )

    assert renamed.status_code == 200
    assert (renamed.json()["label"], renamed.json()["provider"]) == ("Acme", "github")
    found = dockyard.get(f"/api/git-credentials/{credential['id']}", headers=signed_in)
    assert found.json()["label"] == "Acme"


def test_a_user_rotates_the_token_of_a_git_credential_without_ever_seeing_either_token(
    dockyard: TestClient, proxy: AccessProxy, tmp_path: Path
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    credential = add(dockyard, signed_in, label="Work").json()
    new_token = "github_pat_11BBBBBBB0the0token0after0rotation"

    rotated = dockyard.patch(
        f"/api/git-credentials/{credential['id']}", json={"token": new_token}, headers=signed_in
    )

    assert rotated.status_code == 200
    assert rotated.json()["label"] == "Work"
    assert TOKEN not in rotated.text
    assert new_token not in rotated.text
    assert new_token not in dockyard.get("/api/git-credentials", headers=signed_in).text
    for stored in tmp_path.rglob("*"):
        if stored.is_file():
            assert new_token.encode() not in stored.read_bytes(), stored


@pytest.mark.parametrize(
    ("fields", "error"),
    [({"label": ""}, "label_missing"), ({"token": "not a token"}, "token_invalid")],
)
def test_a_rename_or_rotation_with_a_bad_label_or_token_is_refused_and_changes_nothing(
    dockyard: TestClient, proxy: AccessProxy, fields: dict[str, str], error: str
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    credential = add(dockyard, signed_in, label="Work").json()

    refused = dockyard.patch(
        f"/api/git-credentials/{credential['id']}", json=fields, headers=signed_in
    )

    assert refused.status_code == 422
    assert refused.json()["error"] == error
    found = dockyard.get(f"/api/git-credentials/{credential['id']}", headers=signed_in).json()
    assert found == credential


def test_a_user_deletes_a_git_credential(dockyard: TestClient, proxy: AccessProxy) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    kept = add(dockyard, signed_in, label="Kept").json()
    deleted = add(dockyard, signed_in, label="Deleted").json()

    response = dockyard.delete(f"/api/git-credentials/{deleted['id']}", headers=signed_in)

    assert response.status_code == 204
    listed = dockyard.get("/api/git-credentials", headers=signed_in).json()
    assert [c["id"] for c in listed] == [kept["id"]]
    gone = dockyard.get(f"/api/git-credentials/{deleted['id']}", headers=signed_in)
    assert gone.status_code == 404


def test_a_user_never_reaches_another_users_git_credentials(
    dockyard: TestClient, proxy: AccessProxy, add_user: Callable[[str], None]
) -> None:
    add_user(OTHER_USER)
    owner = proxy.headers(ADMINISTRATOR)
    other = proxy.headers(OTHER_USER)
    credential = add(dockyard, owner, label="Mine").json()
    path = f"/api/git-credentials/{credential['id']}"

    assert dockyard.get("/api/git-credentials", headers=other).json() == []
    assert dockyard.get(path, headers=other).status_code == 404
    assert dockyard.patch(path, json={"label": "Stolen"}, headers=other).status_code == 404
    assert dockyard.patch(path, json={"token": "ghp_stolen"}, headers=other).status_code == 404
    assert dockyard.delete(path, headers=other).status_code == 404
    assert dockyard.get(path, headers=owner).json() == credential


@pytest.mark.parametrize(
    "body",
    [
        {"provider": "github", "token": TOKEN},  # no label
        {"label": "Work", "provider": "bitbucket", "token": TOKEN},
        {"label": ["Work"], "provider": "github", "token": TOKEN},
    ],
)
def test_a_malformed_request_is_refused_without_echoing_the_token(
    dockyard: TestClient, proxy: AccessProxy, body: dict[str, object]
) -> None:
    refused = dockyard.post("/api/git-credentials", json=body, headers=proxy.headers(ADMINISTRATOR))

    assert refused.status_code == 422
    assert TOKEN not in refused.text


def test_a_malformed_rotation_is_refused_without_echoing_the_token(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    credential = add(dockyard, signed_in).json()

    refused = dockyard.patch(
        f"/api/git-credentials/{credential['id']}",
        json={"label": 42, "token": TOKEN},
        headers=signed_in,
    )

    assert refused.status_code == 422
    assert TOKEN not in refused.text
