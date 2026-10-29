"""CSRF-protected WebAuthn ceremony endpoints with no-store responses."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from django.http import HttpRequest, HttpResponse, HttpResponseBase, JsonResponse
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_POST
from webauthn import generate_registration_options, options_to_json
from webauthn.helpers import parse_registration_credential_json
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from apps.identity.current_context import (
    CurrentActorError,
    require_current_actor_clinic_roles,
)
from apps.identity.models import Clinic, UserClinicRole
from apps.identity.otp import auth_response, safe_next_url
from apps.identity.passkey_challenges import (
    CHALLENGE_TTL,
    MAX_CREDENTIAL_JSON,
    PasskeyDeniedError,
    consume_challenge,
    deny_passkey,
    new_challenge,
    store_challenge,
)
from apps.identity.passkey_controls import require_registration_session
from apps.identity.passkeys import register_passkey
from apps.identity.session_inventory import (
    SessionInventoryDeniedError,
)


@sensitive_variables()
@sensitive_post_parameters()
@require_POST
def registration_begin(request: HttpRequest) -> HttpResponseBase:
    """Start an own platform credential ceremony after recent authentication."""
    try:
        clinic_id = UUID(request.POST.get("clinic_id", ""))
        user_id = require_current_actor_clinic_roles(
            clinic_id=clinic_id, roles=tuple(UserClinicRole.Role)
        )
        key = request.session.session_key
        if key is None:
            deny_passkey()
        require_registration_session(session_key=key)
        clinic = Clinic.objects.get(pk=clinic_id)
        challenge = replace(
            new_challenge(request, purpose="register"),
            user_id=user_id,
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            target=safe_next_url(request, request.POST.get("next")),
        )
    except (
        ValueError,
        PasskeyDeniedError,
        CurrentActorError,
        SessionInventoryDeniedError,
    ):
        return auth_response(JsonResponse({"code": "passkey_denied"}, status=403))
    options = generate_registration_options(
        rp_id=challenge.rp_id,
        rp_name="Clinic Ops",
        user_name=request.user.get_username(),
        user_id=clinic.organization_id.bytes + user_id.bytes,
        challenge=challenge.challenge,
        timeout=CHALLENGE_TTL * 1000,
        authenticator_selection=AuthenticatorSelectionCriteria(
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    store_challenge(request, challenge=challenge)
    return auth_response(
        HttpResponse(options_to_json(options), content_type="application/json")
    )


@sensitive_variables()
@sensitive_post_parameters()
@require_POST
def registration_finish(request: HttpRequest) -> HttpResponseBase:
    """Consume the exact server challenge and store a verified own credential."""
    try:
        challenge = consume_challenge(request, purpose="register")
        if challenge.clinic_id is None:
            deny_passkey()
        key = request.session.session_key
        if key is None:
            deny_passkey()
        require_registration_session(session_key=key)
        raw = request.POST.get("credential", "")
        if not raw or len(raw) > MAX_CREDENTIAL_JSON:
            deny_passkey()
        credential = parse_registration_credential_json(raw)
        register_passkey(
            clinic_id=challenge.clinic_id,
            credential=credential,
            challenge=challenge,
            nickname=request.POST.get("nickname", "").strip(),
            session_key=key,
        )
    except (
        PasskeyDeniedError,
        SessionInventoryDeniedError,
        WebAuthnException,
        ValueError,
    ):
        return auth_response(JsonResponse({"code": "passkey_denied"}, status=403))
    return auth_response(
        JsonResponse(
            {"code": "passkey_registered", "next": challenge.target}, status=201
        )
    )
