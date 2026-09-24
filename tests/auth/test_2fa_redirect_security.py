from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.identity.models import User
from django.test import Client

from otp_test_support import runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "next_url",
    [
        "https://attacker.invalid/steal",
        "//attacker.invalid/steal",
        "javascript:alert(1)",
        "\\attacker.invalid\\steal",
        "/%61uth/login/",
        "/auth/%76erify/",
        "/auth%2Fverify%2F",
        "/x/../auth/login/",
        "/auth/enroll/../verify/",
        "/%2e%2e/auth/logout/",
        "/auth/login",
        "/auth/enroll",
        "/auth/verify",
        "/auth/logout",
    ],
)
def test_login_rejects_hostile_next_urls(
    rbac_graph: RbacGraph,
    next_url: str,
) -> None:
    username = User.objects.get(pk=rbac_graph.physician).username
    client = Client()

    with runtime_role():
        response = client.post(
            "/auth/login/",
            {
                "username": username,
                "password": RBAC_RAW_CREDENTIAL,
                "next": next_url,
            },
        )

    assert response.status_code == 302
    assert response.headers["Location"] == "/auth/protected/"


def test_login_preserves_a_valid_local_next_path(rbac_graph: RbacGraph) -> None:
    username = User.objects.get(pk=rbac_graph.physician).username
    client = Client()
    target = "/clinical/worklist/?view=mine"

    with runtime_role():
        response = client.post(
            "/auth/login/",
            {
                "username": username,
                "password": RBAC_RAW_CREDENTIAL,
                "next": target,
            },
        )

    assert response.status_code == 302
    assert response.headers["Location"] == target
