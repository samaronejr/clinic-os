"""Protected email invitations ending only at password and confirmed TOTP."""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final, cast
from uuid import UUID, uuid4

from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.audit.services import record_phase1_event
from apps.core.integration import OperationRequest, enqueue_operation
from apps.identity.current_context import require_permission
from apps.identity.invitation_management import expire_pending_invitations
from apps.identity.lifecycle_authority import (
    LifecycleDeniedError,
    require_staff_management,
)
from apps.identity.models import StaffInvitation, UserClinicRole
from apps.tenancy.envelope import protect, reveal

if TYPE_CHECKING:
    from apps.identity.otp import TotpDevice

EMAIL_PROVIDER = "identity-email-v1"
INVITATION_SUBJECT = "identity.staff_invitation"
TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}")
MAX_USERNAME_LENGTH: Final = 150


@dataclass(frozen=True, slots=True)
class InvitationEnrollment:
    """Private device data stays in the immediate enrollment response only."""

    device: TotpDevice
    accepted_user_id: UUID | None


def invitation_token(invitation: StaffInvitation) -> str:
    """Rehydrate one nonce-bound token for protected post-commit email delivery."""
    value = (
        f"{invitation.pk}:{invitation.organization_id}:"
        f"{invitation.pending_user_id}:{invitation.token_nonce}"
    )
    return salted_hmac(
        "identity.staff_invitation", value, algorithm="sha256"
    ).hexdigest()


def token_hash(token: str) -> bytes:
    """Refuse malformed tokens before hashing or any scope lookup."""
    if TOKEN_PATTERN.fullmatch(token) is None:
        raise LifecycleDeniedError
    return hashlib.sha256(token.encode("ascii")).digest()


def invite_staff(*, clinic_id: UUID, email: str, bundle: str) -> StaffInvitation:
    """Create or resume one protected pending email/clinic/bundle invitation."""
    actor_id, clinic = require_staff_management(clinic_id=clinic_id)
    if bundle not in UserClinicRole.Role.values:
        raise LifecycleDeniedError
    if bundle in {UserClinicRole.Role.OWNER, UserClinicRole.Role.ORG_ADMIN}:
        require_permission("staff.organization", clinic_id=clinic_id)
    normalized = email.strip().casefold()
    validate_email(normalized)
    digest = salted_hmac(
        "identity.staff_invitation.email", normalized, algorithm="sha256"
    ).digest()
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [f"staff-invitation:{clinic_id}:{digest.hex()}:{bundle}"],
        )
        expire_pending_invitations(clinic_id=clinic_id)
        existing = (
            StaffInvitation.objects.filter(
                clinic_id=clinic_id,
                email_hash=digest,
                bundle=bundle,
                state=StaffInvitation.State.PENDING,
                expires_at__gt=timezone.now(),
            )
            .order_by("created_at")
            .first()
        )
        if existing is not None:
            return existing
        pending_id = uuid4()
        cursor.execute(
            "SELECT clinic_app.identity_invitation_account(%s, %s)",
            [clinic_id, pending_id],
        )
        if cursor.fetchone() != (pending_id,):
            raise LifecycleDeniedError
        invitation = StaffInvitation(
            id=uuid4(),
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            pending_user_id=pending_id,
            invited_by_id=actor_id,
            email_hash=digest,
            email=normalized,
            bundle=bundle,
            expires_at=timezone.now() + timedelta(days=2),
        )
        invitation.token_hash = token_hash(invitation_token(invitation))
        invitation.save(force_insert=True)
        enqueue_operation(
            request=OperationRequest(
                channel="email",
                provider=EMAIL_PROVIDER,
                clinic_id=clinic_id,
                subject_type=INVITATION_SUBJECT,
                subject_id=invitation.pk,
                idempotency_key=invitation.pk,
            )
        )
        record_phase1_event(
            "identity.staff.invited",
            clinic_id=clinic_id,
            affected_record_id=invitation.pk,
        )
    return invitation


def enroll_invitation(
    *,
    token: str,
    username: str,
    password: str,
    otp_token: str | None = None,
) -> InvitationEnrollment:
    """Resume the same pending account/device; activate only after real TOTP."""
    normalized_username = username.strip()
    UnicodeUsernameValidator()(normalized_username)
    if not normalized_username or len(normalized_username) > MAX_USERNAME_LENGTH:
        message = "Enter a valid username."
        raise ValidationError(message, code="invalid_username")
    validate_password(password)
    digest = token_hash(token)
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM clinic_app.identity_invitation_claim(%s)", [digest]
            )
            row = cursor.fetchone()
            if row is None:
                raise LifecycleDeniedError
            (
                invitation_id,
                organization_id,
                clinic_id,
                pending_user_id,
                desired_username,
                encoded_password,
                device_id,
            ) = row
            cursor.execute(
                "SELECT set_config('app.current_tenant', %s, true), "
                "set_config('app.current_user_id', %s, true)",
                [str(organization_id), str(pending_user_id)],
            )
            try:
                with transaction.atomic():
                    if device_id is None:
                        cursor.execute(
                            "SELECT clinic_app.identity_invitation_password"
                            "(%s, %s, %s, %s)",
                            [
                                digest,
                                protect(
                                    purpose="identity.staff.invitation.username",
                                    plaintext=normalized_username.encode("utf-8"),
                                ),
                                make_password(password),
                                secrets.token_hex(20),
                            ],
                        )
                        result = cursor.fetchone()
                        if result is None or result[0] is None:
                            raise LifecycleDeniedError
                        device_id = result[0]
                    else:
                        if desired_username is None:
                            raise LifecycleDeniedError
                        saved_username = reveal(
                            purpose="identity.staff.invitation.username",
                            envelope=bytes(desired_username),
                        ).decode("utf-8")
                        if saved_username != normalized_username or not check_password(
                            password, encoded_password
                        ):
                            raise LifecycleDeniedError
                    device = cast(
                        "TotpDevice",
                        cast(
                            "object",
                            TOTPDevice.objects.select_for_update().get(
                                pk=device_id, user_id=pending_user_id
                            ),
                        ),
                    )
                    accepted_id = None
                    if otp_token is not None and device.verify_token(otp_token):
                        device.confirmed = True
                        device.save(update_fields=("confirmed",))
                        cursor.execute(
                            "SELECT clinic_app.identity_invitation_accept(%s, %s)",
                            [digest, normalized_username],
                        )
                        completed = cursor.fetchone()
                        if completed is None or completed[0] is None:
                            raise LifecycleDeniedError
                        accepted_id = UUID(str(completed[0]))
                        record_phase1_event(
                            "identity.staff.invitation_accepted",
                            clinic_id=clinic_id,
                            affected_record_id=invitation_id,
                        )
                    return InvitationEnrollment(
                        device=device, accepted_user_id=accepted_id
                    )
            finally:
                cursor.execute(
                    "SELECT set_config('app.current_tenant', '', true), "
                    "set_config('app.current_user_id', '', true)"
                )
    except IntegrityError as error:
        raise LifecycleDeniedError from error
