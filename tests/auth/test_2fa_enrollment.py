from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import PropertyMock, patch

import pytest
from apps.identity.models import User
from django.test import Client
from django.utils.translation import gettext
from django_otp.plugins.otp_totp.models import TOTPDevice

from otp_test_support import (
    fixed_otp_time,
    get_totp_device,
    runtime_role,
    token_for,
    totp_device_exists,
)
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_enrollment_get_is_read_only_and_start_reuses_one_pending_device(
    rbac_graph: RbacGraph,
) -> None:
    username = User.objects.get(pk=rbac_graph.physician).username
    client = Client()

    with runtime_role():
        assert client.login(username=username, password=RBAC_RAW_CREDENTIAL)
        first_get = client.get("/auth/enroll/")
        assert not totp_device_exists(rbac_graph.physician, confirmed=False)
        first_start = client.post("/auth/enroll/", {"action": "start"})
        first_device = get_totp_device(rbac_graph.physician, confirmed=False)
        second_start = client.post("/auth/enroll/", {"action": "start"})
        second_device = get_totp_device(rbac_graph.physician, confirmed=False)

    assert first_get.status_code == 200
    assert first_start.status_code == 200
    assert second_start.status_code == 200
    assert first_device.pk == second_device.pk


def test_enrollment_uses_explicit_device_without_config_url_or_match_token(
    rbac_graph: RbacGraph,
) -> None:
    username = User.objects.get(pk=rbac_graph.physician).username
    client = Client()

    with runtime_role():
        assert client.login(username=username, password=RBAC_RAW_CREDENTIAL)
        with patch.object(
            TOTPDevice,
            "config_url",
            new_callable=PropertyMock,
            side_effect=AssertionError("config_url must remain unused"),
        ):
            started = client.post("/auth/enroll/", {"action": "start"})
        device = get_totp_device(rbac_graph.physician, confirmed=False)
        with (
            fixed_otp_time(),
            patch(
                "django_otp.forms.match_token",
                side_effect=AssertionError("match_token must remain unused"),
            ),
        ):
            confirmed = client.post(
                "/auth/enroll/",
                {
                    "action": "confirm",
                    "otp_device": device.persistent_id,
                    "otp_token": token_for(device),
                },
            )

    assert started.status_code == 200
    assert confirmed.status_code == 302


def test_invalid_token_is_not_redisplayed_and_triggers_backoff(
    rbac_graph: RbacGraph,
) -> None:
    username = User.objects.get(pk=rbac_graph.physician).username
    client = Client()

    with runtime_role():
        assert client.login(username=username, password=RBAC_RAW_CREDENTIAL)
        client.post("/auth/enroll/", {"action": "start"})
        device = get_totp_device(rbac_graph.physician, confirmed=False)
        with fixed_otp_time():
            invalid = client.post(
                "/auth/enroll/",
                {
                    "action": "confirm",
                    "otp_device": device.persistent_id,
                    "otp_token": "987654",
                },
            )
            throttled = client.post(
                "/auth/enroll/",
                {
                    "action": "confirm",
                    "otp_device": device.persistent_id,
                    "otp_token": token_for(device),
                },
            )

    assert invalid.status_code == 200
    assert b"987654" not in invalid.content
    assert throttled.status_code == 200
    assert (
        gettext("Verification temporarily disabled. Try again soon.").encode()
        in throttled.content
    )
    assert not totp_device_exists(rbac_graph.physician, confirmed=True)
