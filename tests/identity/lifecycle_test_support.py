"""Real login, exact-device verification and session receipts for lifecycle tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.identity.models import User
from django.test import Client

from otp_test_support import create_totp_device, fixed_otp_time, runtime_role, token_for

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph


def verified_client(graph: RbacGraph) -> Client:
    """Verify through the existing HTTP form; do not stamp a synthetic session."""
    user = User.objects.get(pk=graph.clinic_admin)
    device = create_totp_device(user.pk, confirmed=True)
    client = Client()
    with runtime_role():
        client.force_login(user)
        with fixed_otp_time():
            response = client.post(
                "/auth/verify/",
                {
                    "otp_device": device.persistent_id,
                    "otp_token": token_for(device),
                    "next": "/auth/protected/",
                },
            )
    assert response.status_code == 302
    return client


def session_key(client: Client) -> str:
    """Return a framework-owned private key without including it in diagnostics."""
    value = client.session.session_key
    assert value is not None
    return value
