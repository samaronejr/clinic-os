from __future__ import annotations

from typing import TYPE_CHECKING, Literal, assert_never

import pytest
from apps.identity import stepup
from apps.identity.models import User, UserClinicRole
from apps.identity.stepup import STEP_UP_SESSION_KEY
from django.contrib.auth.hashers import make_password
from django.test import override_settings
from django_otp import DEVICE_ID_SESSION_KEY

from auth.stepup_test_support import (
    STEP_UP_MAX_AGE,
    STEP_UP_NOW,
    create_role_actor,
    logged_in_client,
    seed_freshness,
)
from otp_test_support import (
    create_totp_device,
    fixed_otp_time,
    runtime_role,
    token_for,
)
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch

    from rbac_fixtures import RbacGraph

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("_fixed_step_up_clock"),
]


@pytest.fixture
def _fixed_step_up_clock(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(stepup, "_utc_now_seconds", lambda: STEP_UP_NOW)


@pytest.mark.parametrize("flow", ["verify", "enroll"])
def test_every_todo9_successful_otp_seam_stamps_freshness(
    rbac_graph: RbacGraph,
    flow: Literal["verify", "enroll"],
) -> None:
    confirmed = flow == "verify"
    device = create_totp_device(rbac_graph.physician, confirmed=confirmed)
    client = logged_in_client(rbac_graph.physician)
    match flow:
        case "verify":
            path = "/auth/verify/"
            payload = {
                "otp_device": device.persistent_id,
                "otp_token": token_for(device),
            }
        case "enroll":
            path = "/auth/enroll/"
            payload = {
                "action": "confirm",
                "otp_device": device.persistent_id,
                "otp_token": token_for(device),
            }
        case unreachable:
            assert_never(unreachable)

    with runtime_role(), fixed_otp_time():
        response = client.post(path, payload)

    assert response.status_code == 302
    assert client.session[STEP_UP_SESSION_KEY] == STEP_UP_NOW


def test_password_relogin_and_another_user_clear_freshness(
    rbac_graph: RbacGraph,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    client = logged_in_client(rbac_graph.physician)
    seed_freshness(client, device)
    physician = User.objects.get(pk=rbac_graph.physician)

    with runtime_role():
        relogin = client.post(
            "/auth/login/",
            {"username": physician.username, "password": RBAC_RAW_CREDENTIAL},
        )

    assert relogin.status_code == 302
    assert STEP_UP_SESSION_KEY not in client.session
    assert DEVICE_ID_SESSION_KEY not in client.session

    admin = User.objects.get(pk=rbac_graph.clinic_admin)
    seed_freshness(client, device)
    with runtime_role():
        switched = client.post(
            "/auth/login/",
            {"username": admin.username, "password": RBAC_RAW_CREDENTIAL},
        )

    assert switched.status_code == 302
    assert STEP_UP_SESSION_KEY not in client.session
    assert DEVICE_ID_SESSION_KEY not in client.session


def test_logout_clears_freshness_and_back_does_not_restore_it(
    rbac_graph: RbacGraph,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    client = logged_in_client(rbac_graph.physician)
    seed_freshness(client, device)

    with runtime_role():
        response = client.post("/auth/logout/")
        back = client.get("/auth/step-up/")

    assert response.status_code == 302
    assert STEP_UP_SESSION_KEY not in client.session
    assert back.status_code == 403


@override_settings(ROOT_URLCONF="stepup_urls")
@pytest.mark.parametrize("invalidator", ["inactive", "password-change"])
def test_inactive_or_password_changed_session_clears_freshness(
    rbac_graph: RbacGraph,
    invalidator: str,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    client = logged_in_client(rbac_graph.physician)
    seed_freshness(client, device)
    update = (
        {"is_active": False}
        if invalidator == "inactive"
        else {"password": make_password("todo10-replacement-password")}
    )
    User.objects.filter(pk=rbac_graph.physician).update(**update)

    with runtime_role():
        response = client.get("/__test__/issuance/")

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/auth/step-up/")
    assert STEP_UP_SESSION_KEY not in client.session


@override_settings(ROOT_URLCONF="stepup_urls")
@pytest.mark.parametrize(
    "role",
    [
        UserClinicRole.Role.OWNER,
        UserClinicRole.Role.CLINIC_ADMIN,
        UserClinicRole.Role.PHYSICIAN,
        UserClinicRole.Role.RECEPTIONIST,
    ],
)
def test_every_role_requires_the_same_step_up_freshness(
    rbac_graph: RbacGraph,
    role: UserClinicRole.Role,
) -> None:
    user = create_role_actor(rbac_graph, role)
    device = create_totp_device(user.pk, confirmed=True)
    client = logged_in_client(user.pk)

    with runtime_role():
        stale = client.get("/__test__/issuance/")
    seed_freshness(client, device, verified_at=STEP_UP_NOW - STEP_UP_MAX_AGE)
    with runtime_role():
        fresh = client.get("/__test__/issuance/")

    assert stale.status_code == 302
    assert stale.headers["Location"].startswith("/auth/step-up/")
    assert fresh.status_code == 200


def test_organization_switch_does_not_clear_user_session_freshness(
    rbac_graph: RbacGraph,
) -> None:
    device = create_totp_device(rbac_graph.shared_user, confirmed=True)
    client = logged_in_client(rbac_graph.shared_user)
    seed_freshness(client, device)
    session = client.session
    session["active_org_id"] = str(rbac_graph.organization_b)
    session.save()

    assert client.session[STEP_UP_SESSION_KEY] == STEP_UP_NOW
    assert client.session["active_org_id"] == str(rbac_graph.organization_b)
