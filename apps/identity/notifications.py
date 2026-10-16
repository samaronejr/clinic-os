"""Protected identity mail prepared in scope; synthetic acceptance is not delivery."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import connection
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.crypto import salted_hmac

from apps.comms.adapters import (
    AuthenticatedCallback,
    CallbackAuthenticationError,
    OperationScope,
    PermanentSendError,
    SendResult,
)
from apps.comms.models import IntegrationOperation
from apps.core.integration import (
    register_callback_authenticator,
    register_send_adapter,
    register_subject_recheck,
)
from apps.identity.invitations import (
    EMAIL_PROVIDER,
    INVITATION_SUBJECT,
    invitation_token,
)
from apps.identity.lifecycle_authority import (
    LifecycleDeniedError,
    require_staff_management,
)
from apps.identity.models import AuthenticatorReset, StaffInvitation
from apps.identity.recovery import RESET_SUBJECT

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

MAX_CALLBACK_BYTES = 8192
MAX_CALLBACK_FIELD = 255
SIGNATURE_HEADER = "X-Clinic-Identity-Signature"
CALLBACK_FIELDS = frozenset({"event_id", "provider_reference", "status"})


@dataclass(frozen=True, slots=True)
class IdentityEmail:
    """Ephemeral content is neither outbox metadata nor diagnostic text."""

    destination: str = field(repr=False)
    body: str = field(repr=False)


def invitation_send_eligible(scope: OperationScope) -> bool:
    """Recheck current manager authority, expiry and pending state before send."""
    try:
        require_staff_management(clinic_id=scope.clinic_id)
    except LifecycleDeniedError:
        return False
    operation = IntegrationOperation.objects.filter(
        pk=scope.operation_id,
        organization_id=scope.organization_id,
        clinic_id=scope.clinic_id,
        subject_type=INVITATION_SUBJECT,
    ).first()
    if operation is None:
        return False
    return StaffInvitation.objects.filter(
        pk=operation.subject_id,
        organization_id=scope.organization_id,
        clinic_id=scope.clinic_id,
        state=StaffInvitation.State.PENDING,
        revoked_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).exists()


def reset_send_eligible(scope: OperationScope) -> bool:
    """Reject a revoked actor/target or finished reset before notification."""
    try:
        require_staff_management(clinic_id=scope.clinic_id)
    except LifecycleDeniedError:
        return False
    reset = AuthenticatorReset.objects.filter(
        notification_operation_id=scope.operation_id,
        organization_id=scope.organization_id,
        clinic_id=scope.clinic_id,
        executed_at__isnull=True,
    ).first()
    if reset is None:
        return False
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.identity_reset_target(%s, %s)",
            [scope.clinic_id, reset.user_id],
        )
        row = cursor.fetchone()
        return row is not None and row[0] is True


class IdentityEmailAdapter:
    """Only explicit synthetic email is enabled; no real transport is selected."""

    provider = EMAIL_PROVIDER

    def prepare(self, operation: IntegrationOperation) -> IdentityEmail:
        """Build private content only inside the operation's authenticated scope."""
        require_staff_management(clinic_id=operation.clinic_id)
        if operation.channel != "email":
            raise PermanentSendError
        if operation.subject_type == INVITATION_SUBJECT:
            invitation = StaffInvitation.objects.filter(
                pk=operation.subject_id,
                organization_id=operation.organization_id,
                clinic_id=operation.clinic_id,
                state=StaffInvitation.State.PENDING,
                expires_at__gt=timezone.now(),
            ).first()
            if invitation is None:
                raise PermanentSendError
            return IdentityEmail(
                destination=invitation.email,
                body=render_to_string(
                    "identity/invitation_email.txt",
                    {"invitation_code": invitation_token(invitation)},
                ).strip(),
            )
        if operation.subject_type != RESET_SUBJECT:
            raise PermanentSendError
        reset = AuthenticatorReset.objects.filter(
            pk=operation.subject_id,
            notification_operation_id=operation.pk,
            organization_id=operation.organization_id,
            clinic_id=operation.clinic_id,
            executed_at__isnull=True,
        ).first()
        if reset is None:
            raise PermanentSendError
        invitation = StaffInvitation.objects.filter(
            pending_user_id=reset.user_id,
            organization_id=operation.organization_id,
            clinic_id=operation.clinic_id,
            state=StaffInvitation.State.ACCEPTED,
        ).first()
        if invitation is not None:
            destination = invitation.email
        else:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT clinic_app.identity_reset_recipient(%s)", [reset.pk]
                )
                row = cursor.fetchone()
            if row is None or not isinstance(row[0], str):
                raise PermanentSendError
            destination = row[0]
        if not destination:
            raise PermanentSendError
        return IdentityEmail(
            destination=destination,
            body=render_to_string("identity/authenticator_reset_email.txt").strip(),
        )

    def send(self, prepared: object, *, operation_id: UUID) -> SendResult:
        """Return synthetic acceptance only; signed callback records delivery."""
        if not isinstance(prepared, IdentityEmail) or "email" not in getattr(
            settings, "COMMS_SYNTHETIC_CHANNELS", ()
        ):
            raise PermanentSendError
        return SendResult(f"synthetic:identity-email:{operation_id}")


class IdentityEmailCallback:
    """Authenticate bounded synthetic receipts before resolving any tenant."""

    provider = EMAIL_PROVIDER

    def authenticate(
        self, *, headers: Mapping[str, str], body: bytes
    ) -> AuthenticatedCallback:
        """Bind the exact raw bytes to the private synthetic callback key."""
        if (
            "email" not in getattr(settings, "COMMS_SYNTHETIC_CHANNELS", ())
            or len(body) > MAX_CALLBACK_BYTES
        ):
            raise CallbackAuthenticationError
        key = salted_hmac(
            "identity.email.callback.key", EMAIL_PROVIDER, algorithm="sha256"
        ).digest()
        expected = hmac.new(key, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(headers.get(SIGNATURE_HEADER, ""), expected):
            raise CallbackAuthenticationError
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CallbackAuthenticationError from error
        if not isinstance(payload, dict) or set(payload) != CALLBACK_FIELDS:
            raise CallbackAuthenticationError
        event_id = payload["event_id"]
        reference = payload["provider_reference"]
        status = payload["status"]
        if (
            not isinstance(event_id, str)
            or not 1 <= len(event_id) <= MAX_CALLBACK_FIELD
            or not isinstance(reference, str)
            or not 1 <= len(reference) <= MAX_CALLBACK_FIELD
            or status not in ("delivered", "failed")
        ):
            raise CallbackAuthenticationError
        return AuthenticatedCallback(event_id, reference, status)


def register_identity_notifications() -> None:
    """Connect the two real subject checks and separately authenticated callback."""
    register_send_adapter(IdentityEmailAdapter())
    register_callback_authenticator(IdentityEmailCallback())
    register_subject_recheck(INVITATION_SUBJECT, invitation_send_eligible)
    register_subject_recheck(RESET_SUBJECT, reset_send_eligible)
