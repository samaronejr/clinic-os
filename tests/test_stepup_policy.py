from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

import pytest
from apps.identity import stepup
from apps.identity.stepup import STEP_UP_SESSION_KEY, StepUpRequired
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.test import override_settings
from django_otp import DEVICE_ID_SESSION_KEY

from otp_test_support import create_totp_device, runtime_role
from stepup_test_support import (
    STEP_UP_MAX_AGE,
    STEP_UP_NOW,
    logged_in_client,
    seed_freshness,
)

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch

    from rbac_fixtures import RbacGraph

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("_step_up_clock"),
]

CAUSAL_DISABLE_ENV: Final = "CLINIC_STEPUP_CAUSAL_DISABLE"
INVALID_MAX_AGE_CASES: Final = (
    "nan",
    "infinity",
    "finite-float",
    "string",
    "bool",
    "negative",
)


@pytest.fixture
def _step_up_clock(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(stepup, "_utc_now_seconds", lambda: STEP_UP_NOW)
    if os.environ.get(CAUSAL_DISABLE_ENV) == "1":
        monkeypatch.setattr(stepup, "_freshness_is_valid", lambda *_args: True)


@override_settings(ROOT_URLCONF="stepup_urls")
@pytest.mark.parametrize("age", [0, STEP_UP_MAX_AGE])
def test_assert_step_up_accepts_fresh_exact_device_at_inclusive_boundary(
    rbac_graph: RbacGraph,
    age: int,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    client = logged_in_client(rbac_graph.physician)
    seed_freshness(client, device, verified_at=STEP_UP_NOW - age)

    with runtime_role():
        response = client.get("/__test__/raw-issuance/")

    assert response.status_code == 200
    assert response.content == b"raw issuance hook reached"


@override_settings(ROOT_URLCONF="stepup_urls")
def test_assert_step_up_rejects_max_age_plus_one_and_clears_freshness(
    rbac_graph: RbacGraph,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    client = logged_in_client(rbac_graph.physician)
    seed_freshness(
        client,
        device,
        verified_at=STEP_UP_NOW - STEP_UP_MAX_AGE - 1,
    )

    with runtime_role():
        response = client.get("/__test__/raw-issuance/")

    assert response.status_code == 403
    assert STEP_UP_SESSION_KEY not in client.session
    assert DEVICE_ID_SESSION_KEY in client.session


@override_settings(ROOT_URLCONF="stepup_urls")
@pytest.mark.parametrize("invalid_case", INVALID_MAX_AGE_CASES)
def test_assert_step_up_rejects_invalid_max_age_and_clears_freshness(
    rbac_graph: RbacGraph,
    invalid_case: str,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    client = logged_in_client(rbac_graph.physician)
    seed_freshness(client, device)

    with runtime_role():
        response = client.get(
            "/__test__/runtime-limit-raw/",
            {"case": invalid_case},
        )

    assert response.status_code == 403
    assert response.content != b"invalid limit action executed"
    assert STEP_UP_SESSION_KEY not in client.session
    assert DEVICE_ID_SESSION_KEY in client.session


@override_settings(ROOT_URLCONF="stepup_urls")
@pytest.mark.parametrize("invalid_case", INVALID_MAX_AGE_CASES)
def test_step_up_decorator_rejects_invalid_max_age_without_executing_action(
    rbac_graph: RbacGraph,
    invalid_case: str,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    client = logged_in_client(rbac_graph.physician)
    seed_freshness(client, device)

    with runtime_role():
        response = client.get(
            "/__test__/runtime-limit-decorator/",
            {"case": invalid_case},
        )

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/auth/step-up/")
    assert response.content != b"invalid limit action executed"
    assert STEP_UP_SESSION_KEY not in client.session
    assert DEVICE_ID_SESSION_KEY in client.session


@override_settings(ROOT_URLCONF="stepup_urls")
@pytest.mark.parametrize(
    "invalid_value",
    [True, "2100000000", -1, STEP_UP_NOW + 1],
    ids=["bool", "malformed", "negative", "future"],
)
def test_assert_step_up_rejects_invalid_timestamp_shapes(
    rbac_graph: RbacGraph,
    invalid_value: bool | str | int,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    client = logged_in_client(rbac_graph.physician)
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session[STEP_UP_SESSION_KEY] = invalid_value
    session.save()

    with runtime_role():
        response = client.get("/__test__/raw-issuance/")

    assert response.status_code == 403
    assert STEP_UP_SESSION_KEY not in client.session


@override_settings(ROOT_URLCONF="stepup_urls")
def test_timestamp_without_verified_device_fails_and_clears_freshness(
    rbac_graph: RbacGraph,
) -> None:
    client = logged_in_client(rbac_graph.physician)
    session = client.session
    session[STEP_UP_SESSION_KEY] = STEP_UP_NOW
    session.save()

    with runtime_role():
        response = client.get("/__test__/raw-issuance/")

    assert response.status_code == 403
    assert STEP_UP_SESSION_KEY not in client.session


@override_settings(ROOT_URLCONF="stepup_urls")
def test_verified_device_without_timestamp_fails_closed(
    rbac_graph: RbacGraph,
) -> None:
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    client = logged_in_client(rbac_graph.physician)
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()

    with runtime_role():
        response = client.get("/__test__/raw-issuance/")

    assert response.status_code == 403
    assert STEP_UP_SESSION_KEY not in client.session
    assert DEVICE_ID_SESSION_KEY in client.session


@override_settings(ROOT_URLCONF="stepup_urls")
@pytest.mark.parametrize("device_case", ["foreign", "unconfirmed"])
def test_foreign_or_unconfirmed_persistent_device_fails_and_is_cleared(
    rbac_graph: RbacGraph,
    device_case: str,
) -> None:
    user_id = (
        rbac_graph.clinic_admin if device_case == "foreign" else rbac_graph.physician
    )
    device = create_totp_device(
        user_id,
        confirmed=device_case == "foreign",
    )
    client = logged_in_client(rbac_graph.physician)
    seed_freshness(client, device)

    with runtime_role():
        response = client.get("/__test__/raw-issuance/")

    assert response.status_code == 403
    assert STEP_UP_SESSION_KEY not in client.session
    assert DEVICE_ID_SESSION_KEY not in client.session


def test_step_up_error_is_a_typed_permission_exception() -> None:
    assert issubclass(StepUpRequired, PermissionDenied)


def test_explicit_step_up_setting_is_the_public_default() -> None:
    assert settings.STEP_UP_MAX_AGE_SECONDS == STEP_UP_MAX_AGE
    assert stepup.DEFAULT_STEP_UP_MAX_AGE_SECONDS == STEP_UP_MAX_AGE
