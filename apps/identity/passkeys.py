"""Own-user passkey registration through real WebAuthn verification."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from webauthn import verify_registration_response
from webauthn.helpers import parse_client_data_json
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import AttestationFormat

from apps.audit.services import record_phase1_event
from apps.identity.current_context import (
    CurrentActorError,
    require_current_actor_clinic_roles,
)
from apps.identity.models import Clinic, Passkey, UserClinicRole
from apps.identity.passkey_challenges import (
    MAX_NICKNAME,
    MAX_TRANSPORTS,
    Challenge,
    PasskeyDeniedError,
)
from apps.identity.passkey_controls import require_registration_session

if TYPE_CHECKING:
    from uuid import UUID

    from webauthn.helpers.structs import RegistrationCredential


def register_passkey(
    *,
    clinic_id: UUID,
    credential: RegistrationCredential,
    challenge: Challenge,
    nickname: str,
    session_key: str,
) -> Passkey:
    """Bind verified public material only to the current GUC-bound staff user."""
    try:
        user_id = require_current_actor_clinic_roles(
            clinic_id=clinic_id, roles=tuple(UserClinicRole.Role)
        )
    except CurrentActorError as error:
        raise PasskeyDeniedError from error
    require_registration_session(session_key=session_key)
    clinic = Clinic.objects.get(pk=clinic_id)
    if (
        challenge.purpose != "register"
        or challenge.user_id != user_id
        or challenge.organization_id != clinic.organization_id
        or challenge.clinic_id != clinic_id
        or len(nickname) > MAX_NICKNAME
        or "\x00" in nickname
        or len(credential.response.transports or ()) > MAX_TRANSPORTS
    ):
        raise PasskeyDeniedError
    try:
        if parse_client_data_json(credential.response.client_data_json).cross_origin:
            raise PasskeyDeniedError
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge.challenge,
            expected_rp_id=challenge.rp_id,
            expected_origin=challenge.origin,
            require_user_verification=True,
        )
    except (WebAuthnException, ValueError) as error:
        raise PasskeyDeniedError from error
    if (
        verified.fmt != AttestationFormat.NONE
        or verified.credential_id != credential.raw_id
    ):
        raise PasskeyDeniedError
    try:
        with transaction.atomic():
            passkey = Passkey.objects.create(
                organization_id=clinic.organization_id,
                clinic_id=clinic_id,
                user_id=user_id,
                credential_id=verified.credential_id,
                public_key=verified.credential_public_key,
                sign_count=verified.sign_count,
                transports=[
                    item.value for item in credential.response.transports or ()
                ],
                nickname=nickname,
            )
            record_phase1_event(
                "identity.passkey.registered",
                clinic_id=clinic_id,
                affected_record_id=passkey.pk,
            )
    except IntegrityError as error:
        raise PasskeyDeniedError from error
    return passkey
