"""An Administrator invites people by email; an invited person becomes a User on first access."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from .conftest import ADMINISTRATOR, AccessProxy

INVITED = "invited.person@example.com"


def invite(dockyard: TestClient, proxy: AccessProxy, email: str, by: str = ADMINISTRATOR) -> Any:
    return dockyard.post("/api/invitations", json={"email": email}, headers=proxy.headers(by))


def test_an_invited_person_becomes_a_user_on_first_access(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    assert dockyard.get("/api/me", headers=proxy.headers(INVITED)).status_code == 403

    invited = invite(dockyard, proxy, INVITED)

    assert invited.status_code == 201
    assert invited.json()["email"] == INVITED
    me = dockyard.get("/api/me", headers=proxy.headers(INVITED))
    assert me.status_code == 200
    assert me.json() == {"email": INVITED, "role": "user"}


def test_a_user_who_is_not_an_administrator_cannot_invite(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    invite(dockyard, proxy, INVITED)

    refused = invite(dockyard, proxy, "friend@example.com", by=INVITED)

    assert refused.status_code == 403
    assert refused.json()["error"] == "administrator_only"
    assert "Administrator" in refused.json()["message"]
    assert dockyard.get("/api/me", headers=proxy.headers("friend@example.com")).status_code == 403


@pytest.mark.parametrize(
    ("email", "status", "error", "message"),
    [
        ("not-an-email", 422, "email_invalid", "email address"),
        ("two@at@example.com", 422, "email_invalid", "email address"),
        ("someone @example.com", 422, "email_invalid", "email address"),
        ("", 422, "email_invalid", "email address"),
        (ADMINISTRATOR.upper(), 409, "already_user", "already a User"),
        (INVITED, 409, "already_invited", "already invited"),
    ],
)
def test_an_invitation_that_cannot_be_made_is_refused_with_a_reason(
    dockyard: TestClient, proxy: AccessProxy, email: str, status: int, error: str, message: str
) -> None:
    invite(dockyard, proxy, INVITED)

    refused = invite(dockyard, proxy, email)

    assert refused.status_code == status
    assert refused.json()["error"] == error
    assert message in refused.json()["message"]


def test_the_administrator_sees_users_and_pending_invitations(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    invite(dockyard, proxy, "  Pending.Person@Example.com ")
    invite(dockyard, proxy, INVITED)
    dockyard.get("/", headers=proxy.headers(INVITED))  # first access

    invitations = dockyard.get("/api/invitations", headers=signed_in)
    users = dockyard.get("/api/users", headers=signed_in)

    assert [i["email"] for i in invitations.json()] == ["pending.person@example.com"]
    assert [(u["email"], u["role"]) for u in users.json()] == [
        (ADMINISTRATOR, "administrator"),
        (INVITED, "user"),
    ]


def test_a_revoked_invitation_no_longer_lets_the_person_in(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    signed_in = proxy.headers(ADMINISTRATOR)
    invite(dockyard, proxy, INVITED)

    revoked = dockyard.delete(f"/api/invitations/{INVITED.upper()}", headers=signed_in)

    assert revoked.status_code == 204
    assert dockyard.get("/api/invitations", headers=signed_in).json() == []
    refused = dockyard.get("/api/me", headers=proxy.headers(INVITED))
    assert refused.status_code == 403
    assert refused.json()["error"] == "not_invited"


def test_revoking_an_invitation_that_does_not_exist_is_not_found(
    dockyard: TestClient, proxy: AccessProxy
) -> None:
    gone = dockyard.delete(f"/api/invitations/{INVITED}", headers=proxy.headers(ADMINISTRATOR))

    assert gone.status_code == 404
