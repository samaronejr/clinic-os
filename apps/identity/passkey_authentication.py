"""Verified assertion boundaries; a sessionless lookup never grants authority."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.db import connection
from webauthn import verify_authentication_response
from webauthn.helpers import parse_client_data_json
from webauthn.helpers.exceptions import WebAuthnException

from apps.audit.services import record_phase1_event
from apps.identity.current_context import (
    CurrentActorError,
    require_current_actor_clinic_roles,
)
from apps.identity.models import Passkey, UserClinicRole
from apps.identity.passkey_challenges import (
    MAX_CREDENTIAL_ID,
    Challenge,
    PasskeyDeniedError,
)

if TYPE_CHECKING:
    from uuid import UUID

    from webauthn.helpers.structs import AuthenticationCredential


@dataclass(frozen=True, slots=True)
class PasskeyAccount:
    """Candidate identity from stored scope; only a valid signature admits it."""

    passkey_id: UUID
    user_id: UUID
    organization_id: UUID
    clinic_id: UUID
    username: str = field(repr=False)
    encoded_password: str = field(repr=False)


def lookup_credential(*, credential_id: bytes) -> PasskeyAccount:
    """Resolve a credential's active stored account without choosing a tenant."""
    if not 1 <= len(credential_id) <= MAX_CREDENTIAL_ID:
        raise PasskeyDeniedError
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.identity_passkey_lookup(%s)", [credential_id]
        )
        row = cursor.fetchone()
    if row is None:
        raise PasskeyDeniedError
    return PasskeyAccount(*row)


def verify_assertion(
    *,
    credential: AuthenticationCredential,
    challenge: Challenge,
    clinic_id: UUID,
) -> Passkey:
    """Verify actual signed bytes while holding the current own credential lock."""
    try:
        user_id = require_current_actor_clinic_roles(
            clinic_id=clinic_id, roles=tuple(UserClinicRole.Role)
        )
    except CurrentActorError as error:
        raise PasskeyDeniedError from error
    passkey = (
        Passkey.objects.select_for_update()
        .defer("nickname")
        .filter(
            credential_id=credential.raw_id,
            user_id=user_id,
            removed_at__isnull=True,
        )
        .first()
    )
    if (
        passkey is None
        or passkey.organization_id != challenge.organization_id
        or user_id != challenge.user_id
        or (credential.response.user_handle is None and challenge.purpose == "login")
        or (
            credential.response.user_handle is not None
            and credential.response.user_handle
            != passkey.organization_id.bytes + user_id.bytes
        )
    ):
        raise PasskeyDeniedError
    try:
        if parse_client_data_json(credential.response.client_data_json).cross_origin:
            raise PasskeyDeniedError
        # Verify the signature before attributing a counter regression to this
        # credential; unsigned counter bytes cannot manufacture its audit event.
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge.challenge,
            expected_rp_id=challenge.rp_id,
            expected_origin=challenge.origin,
            credential_public_key=bytes(passkey.public_key),
            credential_current_sign_count=0,
            require_user_verification=True,
        )
    except (WebAuthnException, ValueError) as error:
        raise PasskeyDeniedError from error
    if (
        verified.new_sign_count > 0 or passkey.sign_count > 0
    ) and verified.new_sign_count <= passkey.sign_count:
        record_phase1_event(
            "identity.passkey.counter_regression",
            clinic_id=clinic_id,
            affected_record_id=passkey.pk,
        )
        raise PasskeyDeniedError
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.identity_passkey_use(%s, %s)",
            [passkey.pk, verified.new_sign_count],
        )
        if cursor.fetchone() != (True,):
            raise PasskeyDeniedError
    record_phase1_event(
        "identity.passkey.verified",
        clinic_id=clinic_id,
        affected_record_id=passkey.pk,
    )
    return passkey
