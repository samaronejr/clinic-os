"""Independently authenticated sign-in and own-session step-up ceremonies."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from django.contrib.auth import SESSION_KEY, login
from django.http import HttpRequest, HttpResponse, HttpResponseBase, JsonResponse
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_POST
from webauthn import generate_authentication_options, options_to_json
from webauthn.helpers import parse_authentication_credential_json
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    PublicKeyCredentialDescriptor,
    UserVerificationRequirement,
)

from apps.identity.models import Organization, Passkey, User
from apps.identity.otp import auth_response, safe_next_url
from apps.identity.passkey_authentication import lookup_credential, verify_assertion
from apps.identity.passkey_challenges import (
    CHALLENGE_TTL,
    MAX_CREDENTIAL_JSON,
    Ceremony,
    PasskeyDeniedError,
    consume_challenge,
    deny_passkey,
    new_challenge,
    store_challenge,
)
from apps.identity.passkey_sessions import record_passkey_verification
from apps.identity.stepup import clear_step_up_intent, clear_step_up_verification
from apps.tenancy.db import TenantAccessDeniedError, tenant_context


@sensitive_variables()
@sensitive_post_parameters()
@require_POST
def assertion_begin(request: HttpRequest) -> HttpResponseBase:
    """Offer a discoverable sign-in or a policy-approved own-user assertion."""
    try:
        purpose = request.POST.get("purpose", "login")
        allowed: list[PublicKeyCredentialDescriptor] = []
        match purpose:
            case "login":
                if SESSION_KEY in request.session:
                    deny_passkey()
                challenge = new_challenge(request, purpose="login")
            case "step_up":
                user_id = UUID(request.session.get(SESSION_KEY, ""))
                organization_id = UUID(request.session.get("active_org_id", ""))
                with tenant_context(user_id, organization_id):
                    if not Organization.objects.filter(
                        pk=organization_id, passkey_step_up_enabled=True
                    ).exists():
                        deny_passkey()
                    allowed = [
                        PublicKeyCredentialDescriptor(id=bytes(value))
                        for value in Passkey.objects.filter(
                            user_id=user_id, removed_at__isnull=True
                        ).values_list("credential_id", flat=True)
                    ]
                    if not allowed:
                        deny_passkey()
                challenge = replace(
                    new_challenge(request, purpose="step_up"),
                    user_id=user_id,
                    organization_id=organization_id,
                )
            case _:
                deny_passkey()
        challenge = replace(
            challenge,
            target=safe_next_url(request, request.POST.get("next")),
        )
        options = generate_authentication_options(
            rp_id=challenge.rp_id,
            challenge=challenge.challenge,
            timeout=CHALLENGE_TTL * 1000,
            allow_credentials=allowed,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        store_challenge(request, challenge=challenge)
    except (PasskeyDeniedError, TenantAccessDeniedError, ValueError, TypeError):
        return auth_response(JsonResponse({"code": "passkey_denied"}, status=403))
    return auth_response(
        HttpResponse(options_to_json(options), content_type="application/json")
    )


@sensitive_variables()
@sensitive_post_parameters()
@require_POST
def assertion_finish(request: HttpRequest) -> HttpResponseBase:
    """Consume the server challenge before signature checking and session rotation."""
    try:
        purpose: Ceremony = "step_up" if SESSION_KEY in request.session else "login"
        challenge = consume_challenge(request, purpose=purpose)
        raw = request.POST.get("credential", "")
        if not raw or len(raw) > MAX_CREDENTIAL_JSON:
            deny_passkey()
        credential = parse_authentication_credential_json(raw)
        account = lookup_credential(credential_id=credential.raw_id)
        if challenge.purpose == "step_up" and (
            account.user_id != challenge.user_id
            or account.organization_id != challenge.organization_id
        ):
            deny_passkey()
        challenge = replace(
            challenge,
            user_id=account.user_id,
            organization_id=account.organization_id,
            clinic_id=account.clinic_id,
        )
        with tenant_context(account.user_id, account.organization_id):
            # Catch inside this transaction so a verified counter-regression
            # refusal retains its metadata-only audit event.
            try:
                if challenge.purpose == "step_up" and not (
                    Organization.objects.filter(
                        pk=account.organization_id, passkey_step_up_enabled=True
                    ).exists()
                ):
                    deny_passkey()
                passkey = verify_assertion(
                    credential=credential,
                    challenge=challenge,
                    clinic_id=account.clinic_id,
                )
                if challenge.purpose == "login":
                    clear_step_up_verification(request, clear_device=True)
                    user = User(
                        id=account.user_id,
                        username=account.username,
                        password=account.encoded_password,
                        is_active=True,
                    )
                    login(
                        request,
                        user,
                        backend="apps.identity.auth_backends.ClinicBackend",
                    )
                    request.session["active_org_id"] = str(account.organization_id)
                record_passkey_verification(request, passkey_id=passkey.pk)
                clear_step_up_intent(request)
            except PasskeyDeniedError:
                return auth_response(
                    JsonResponse({"code": "passkey_denied"}, status=403)
                )
    except (
        PasskeyDeniedError,
        TenantAccessDeniedError,
        WebAuthnException,
        ValueError,
        TypeError,
    ):
        return auth_response(JsonResponse({"code": "passkey_denied"}, status=403))
    return auth_response(
        JsonResponse({"code": "passkey_verified", "next": challenge.target})
    )
