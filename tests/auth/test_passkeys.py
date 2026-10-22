"""Todo 16: real-role WebAuthn registration, assertion and policy boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.tenancy.db import tenant_context
from django.db import connection
from django.test import override_settings

from auth.passkey_test_support import SoftwareAuthenticator
from identity.lifecycle_test_support import verified_client
from otp_test_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@override_settings(
    WEBAUTHN_RP_ID="testserver",
    WEBAUTHN_ORIGIN="http://testserver",
)
def test_registration_requests_verified_platform_passkey(
    rbac_graph: RbacGraph,
) -> None:
    # Given a staff session with actual, current TOTP verification.
    client = verified_client(rbac_graph)
    # When that user starts registration in their own clinic.
    with runtime_role():
        response = client.post(
            "/auth/passkey/register/begin/",
            {"clinic_id": str(rbac_graph.clinic_b)},
        )
    # Then the ceremony requires user verification, without attestation.
    assert response.status_code == 200
    options = response.json()
    assert options["rp"]["id"] == "testserver"
    assert options["attestation"] == "none"
    assert options["authenticatorSelection"] == {
        "authenticatorAttachment": "platform",
        "residentKey": "required",
        "requireResidentKey": True,
        "userVerification": "required",
    }
    assert "challenge" in options
    assert "no-store" in response.headers["Cache-Control"]


@override_settings(
    WEBAUTHN_RP_ID="testserver",
    WEBAUTHN_ORIGIN="http://testserver",
)
def test_verified_registration_retains_only_public_material_and_encrypted_name(
    rbac_graph: RbacGraph,
) -> None:
    # Given an actual verified staff session and software platform authenticator.
    client = verified_client(rbac_graph)
    authenticator = SoftwareAuthenticator.create()
    with runtime_role():
        begin = client.post(
            "/auth/passkey/register/begin/",
            {"clinic_id": str(rbac_graph.clinic_b)},
        )
        options = begin.json()
        # When the real attestation is submitted through the finish endpoint.
        finish = client.post(
            "/auth/passkey/register/finish/",
            {
                "credential": authenticator.registration(
                    challenge=options["challenge"], rp_id="testserver"
                ),
                "nickname": "Synthetic platform device",
            },
        )
    # Then the stored key is public, scoped and the nickname is not plaintext.
    assert finish.status_code == 201
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT credential_id, public_key, sign_count, nickname "
            "FROM clinic_app.identity_passkey WHERE user_id = %s",
            [rbac_graph.clinic_admin],
        )
        row = cursor.fetchone()
    assert row is not None
    assert bytes(row[0]) == authenticator.credential_id
    assert len(bytes(row[1])) > 32
    assert row[2] == 0
    assert b"Synthetic platform device" not in bytes(row[3])
