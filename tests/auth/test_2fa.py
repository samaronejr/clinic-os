from __future__ import annotations

from typing import TYPE_CHECKING, Literal, assert_never

import pytest
from apps.identity.models import User
from django.test import Client

from otp_test_support import (
    create_receptionist,
    create_totp_device,
    fixed_otp_time,
    get_totp_device,
    login,
    runtime_role,
    token_for,
    totp_device_exists,
)
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("actor", ["physician", "clinic_admin"])
def test_privileged_user_without_confirmed_totp_is_sent_to_enrollment(
    rbac_graph: RbacGraph,
    actor: Literal["physician", "clinic_admin"],
) -> None:
    client = Client()
    match actor:
        case "physician":
            user_id = rbac_graph.physician
        case "clinic_admin":
            user_id = rbac_graph.clinic_admin
        case unreachable:
            assert_never(unreachable)
    username = User.objects.get(pk=user_id).username

    with runtime_role():
        assert client.login(username=username, password=RBAC_RAW_CREDENTIAL)
        response = client.get("/auth/protected/")

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/auth/enroll/")


def test_enrollment_and_verification_grant_privileged_access(
    rbac_graph: RbacGraph,
) -> None:
    client = Client()
    username = User.objects.get(pk=rbac_graph.physician).username

    with runtime_role():
        assert client.login(
            username=username,
            password=RBAC_RAW_CREDENTIAL,
        )
        enrollment = client.get("/auth/enroll/")
        started = client.post(
            "/auth/enroll/",
            {"action": "start", "next": "/auth/protected/"},
        )
        device = get_totp_device(rbac_graph.physician, confirmed=False)
        with fixed_otp_time():
            confirmation = client.post(
                "/auth/enroll/",
                {
                    "otp_device": device.persistent_id,
                    "otp_token": token_for(device),
                    "next": "/auth/protected/",
                },
            )
        protected = client.get("/auth/protected/")

    confirmed_device = get_totp_device(rbac_graph.physician, confirmed=True)
    assert enrollment.status_code == 200
    assert b"data:image/png;base64," not in enrollment.content
    assert started.status_code == 200
    assert b"data:image/png;base64," in started.content
    assert device.key.encode() not in started.content
    assert b"otpauth://" not in started.content
    assert confirmation.status_code == 302
    assert confirmation.headers["Location"] == "/auth/protected/"
    assert confirmed_device.pk == device.pk
    assert protected.status_code == 200


def test_unconfirmed_device_never_satisfies_privileged_enforcement(
    rbac_graph: RbacGraph,
) -> None:
    create_totp_device(rbac_graph.clinic_admin, confirmed=False)
    client = Client()
    username = User.objects.get(pk=rbac_graph.clinic_admin).username

    with runtime_role():
        assert client.login(username=username, password=RBAC_RAW_CREDENTIAL)
        response = client.get("/auth/protected/")

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/auth/enroll/")


def test_nonprivileged_user_reaches_protected_view_without_totp(
    rbac_graph: RbacGraph,
) -> None:
    user = create_receptionist(rbac_graph)
    client = Client()

    with runtime_role():
        response = login(client, user.username)
        protected = client.get("/auth/protected/")

    assert response.status_code == 302
    assert protected.status_code == 200
    assert not totp_device_exists(user.pk, confirmed=True)


def test_login_sets_deterministic_active_organization(
    rbac_graph: RbacGraph,
) -> None:
    user = create_receptionist(rbac_graph)
    client = Client()

    with runtime_role():
        response = login(client, user.username)

    assert response.status_code == 302
    assert response.headers["Location"] == "/auth/protected/"
    assert client.session["active_org_id"] == str(rbac_graph.organization_a)
