"""Per-attempt professional verification for the future issuance boundary.

Call inside the authenticated tenant transaction immediately before signing;
never reuse the returned evidence as authorization for another attempt. The
prescription domain is still a stub. This service does not issue documents or
claim certificate verification. Catch PhysicianVerificationRequired inside the
tenant transaction to retain the failed attempt, as with clinical denials.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Final

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.utils.timezone import now as utc_now

from apps.ehr.models import Encounter
from apps.identity.current_context import (
    CurrentActorError,
    require_current_actor_clinic_roles,
)
from apps.identity.models import PhysicianEvidence, PhysicianProfile, UserClinicRole
from apps.identity.physician_registry import (
    RegistrationIdentity,
    RegistryResponse,
    RegistryUnavailableError,
    SigningIdentity,
    get_physician_registry,
    registry_capability,
)
from apps.identity.stepup import assert_step_up

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest


MAX_REFERENCE_LENGTH: Final = 255


class PhysicianVerificationRequired(PermissionDenied):
    """Recoverable, non-sensitive machine state; no patient/provider payload."""

    def __init__(self, reason_code: str) -> None:
        """Expose the exact corrective boundary without granting authority."""
        self.reason_code = reason_code
        super().__init__(reason_code)


def _assigned_encounter(
    request: HttpRequest, clinic_id: UUID, encounter_id: UUID
) -> Encounter:
    try:
        actor = require_current_actor_clinic_roles(
            clinic_id, (UserClinicRole.Role.PHYSICIAN,)
        )
    except CurrentActorError as error:
        reason = "clinical_authority_required"
        raise PhysicianVerificationRequired(reason) from error
    if not request.user.is_authenticated or request.user.pk != actor:
        reason = "authenticated_issuer_mismatch"
        raise PhysicianVerificationRequired(reason)
    encounter = (
        Encounter.objects.select_related("appointment", "clinic")
        .filter(pk=encounter_id, clinic_id=clinic_id)
        .first()
    )
    if (
        encounter is None
        or encounter.physician_id != actor
        or encounter.appointment.practitioner_id != actor
    ):
        reason = "assigned_physician_required"
        raise PhysicianVerificationRequired(reason)
    return encounter


def _response_reason(response: RegistryResponse, expected: RegistrationIdentity) -> str:
    """Reject invalid external evidence before persisting its bounded fields."""
    if not response.synthetic:
        return "registry_response_invalid"
    if response.identity.jurisdiction != expected.jurisdiction:
        return "registration_jurisdiction_mismatch"
    if response.identity.registration_number != expected.registration_number:
        return "registration_number_mismatch"
    if response.identity.signing_subject != expected.signing_subject:
        return "registry_identity_mismatch"
    if not response.reference or len(response.reference) > MAX_REFERENCE_LENGTH:
        return "registry_response_invalid"
    return _registration_state(response)


def _registration_state(response: RegistryResponse) -> str:
    if response.status not in PhysicianProfile.Status.values:
        return "registration_unknown"
    times = [response.checked_at, response.recheck_at]
    if response.expires_at is not None:
        times.append(response.expires_at)
    if any(timezone.is_naive(value) for value in times):
        return "registry_response_invalid"
    now = utc_now()
    if (
        response.checked_at > now
        or response.recheck_at <= now
        or response.recheck_at > response.checked_at + timedelta(minutes=5)
    ):
        return "registration_evidence_stale"
    if response.expires_at is not None and response.expires_at <= now:
        return "registration_expired"
    if response.status != PhysicianProfile.Status.REGULAR:
        return f"registration_{response.status}"
    return "verified_synthetic"


def _record_attempt(
    profile: PhysicianProfile,
    encounter: Encounter,
    provider: str,
    response: RegistryResponse | None,
    reason: str,
) -> PhysicianEvidence:
    # Invalid/mismatched responses are not copied into an identity history.
    valid = response is not None and reason not in {
        "registry_response_invalid",
        "registration_jurisdiction_mismatch",
        "registration_number_mismatch",
        "registry_identity_mismatch",
    }
    with transaction.atomic():
        evidence = PhysicianEvidence.objects.create(
            profile=profile,
            encounter=encounter,
            provider=provider,
            synthetic=True,
            reason_code=reason,
            status=(
                response.status
                if valid
                and response is not None
                and response.status in PhysicianProfile.Status.values
                else "unavailable"
            ),
            reference=response.reference if valid and response else "",
            jurisdiction=response.identity.jurisdiction if valid and response else "",
            registration_number=response.identity.registration_number
            if valid and response
            else "",
            signing_subject=response.identity.signing_subject
            if valid and response
            else "",
            checked_at=response.checked_at if valid and response else None,
            expires_at=response.expires_at if valid and response else None,
            recheck_at=response.recheck_at if valid and response else None,
        )
        status = reason.removeprefix("registration_")
        if reason == "verified_synthetic":
            status = "regular"
        elif status not in PhysicianProfile.Status.values:
            status = "unavailable"
        PhysicianProfile.objects.filter(pk=profile.pk).update(
            status=status,
            last_checked_at=evidence.checked_at,
            expires_at=evidence.expires_at,
            recheck_at=evidence.recheck_at,
        )
    return evidence


def verify_physician_for_signing(
    *,
    request: HttpRequest,
    clinic_id: UUID,
    encounter_id: UUID,
    signer: SigningIdentity,
    synthetic: bool = False,
) -> PhysicianEvidence:
    """Check exact actor/assignment, recent TOTP, signer and fresh registry response.

    ``signer`` is trusted signing-adapter output, not a user-selectable credential.
    Synthetic mode is opt-in at both the call and server capability boundaries.
    Real/sandbox verification remains unavailable until task-6 approval exists.
    """
    encounter = _assigned_encounter(request, clinic_id, encounter_id)
    assert_step_up(request)
    capability = registry_capability()
    if not synthetic or not capability.synthetic_enabled:
        raise PhysicianVerificationRequired(capability.reason)
    profile = PhysicianProfile.objects.filter(
        organization_id=encounter.organization_id,
        user_id=encounter.physician_id,
        jurisdiction=encounter.clinic.crm_uf,
    ).first()
    if profile is None:
        reason = "registration_profile_required"
        raise PhysicianVerificationRequired(reason)
    if not profile.synthetic or not signer.synthetic:
        reason = "synthetic_identity_required"
        raise PhysicianVerificationRequired(reason)
    if signer.issuer_id != request.user.pk or signer.subject != profile.signing_subject:
        reason = "signing_identity_mismatch"
        raise PhysicianVerificationRequired(reason)
    identity = RegistrationIdentity(
        profile.jurisdiction, profile.registration_number, profile.signing_subject
    )
    response = None
    provider = "unavailable"
    try:
        registry = get_physician_registry()
        provider = registry.provider
        response = registry.lookup(identity)
    except RegistryUnavailableError:
        reason = "registry_unavailable"
    else:
        reason = _response_reason(response, identity)
    evidence = _record_attempt(profile, encounter, provider, response, reason)
    if reason != "verified_synthetic":
        raise PhysicianVerificationRequired(reason)
    return evidence
