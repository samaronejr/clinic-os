"""Once-shown digest-only recovery and independently notified delayed reset."""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.db import connection, transaction
from django_otp.plugins.otp_static.models import StaticDevice

from apps.audit.services import record_phase1_event
from apps.core.integration import OperationRequest, enqueue_operation
from apps.identity.current_context import (
    current_actor_id,
    require_current_actor_clinic_roles,
)
from apps.identity.invitations import EMAIL_PROVIDER
from apps.identity.lifecycle_authority import (
    LifecycleDeniedError,
    require_staff_management,
)
from apps.identity.models import (
    AuthenticatorReset,
    RecoveryBatch,
    RecoveryCode,
    SessionRecord,
    UserClinicRole,
)
from apps.identity.session_inventory import (
    require_session_step_up,
    require_verified_session,
    session_hash,
)

if TYPE_CHECKING:
    from uuid import UUID

RESET_SUBJECT = "identity.authenticator_reset"
RECOVERY_CODE_PATTERN = re.compile(r"[0-9a-f]{32}")
RECOVERY_CODE_COUNT = 10


@dataclass(frozen=True, slots=True)
class OnceShownRecoveryCodes:
    """Code plaintext belongs only to this immediate no-store response."""

    batch_id: UUID
    codes: tuple[str, ...] = field(repr=False)


def _recovery_hash(*, user_id: UUID, code: str) -> bytes:
    return hashlib.sha256(f"{user_id}:{code}".encode("ascii")).digest()


def issue_recovery_codes(
    *, clinic_id: UUID, session_key: str
) -> OnceShownRecoveryCodes:
    """Atomically issue ten codes once after the user's actual fresh TOTP."""
    user_id = current_actor_id()
    require_current_actor_clinic_roles(
        clinic_id=clinic_id, roles=tuple(UserClinicRole.Role)
    )
    require_verified_session(session_key=session_key)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [f"clinic-lock-v1:user:{user_id}"],
        )
        if RecoveryBatch.objects.filter(
            user_id=user_id, revoked_at__isnull=True
        ).exists():
            raise LifecycleDeniedError
        session = (
            SessionRecord.objects.select_for_update()
            .filter(
                user_id=user_id,
                session_key_hash=session_hash(session_key),
                revoked_at__isnull=True,
            )
            .first()
        )
        if session is None or session.verification_device_id is None:
            raise LifecycleDeniedError
        static_device = StaticDevice.objects.create(
            user_id=user_id, name="Clinic OS recovery codes", confirmed=True
        )
        batch = RecoveryBatch.objects.create(
            user_id=user_id,
            device_id=session.verification_device_id,
            static_device=static_device,
        )
        codes = tuple(secrets.token_hex(16) for _ in range(RECOVERY_CODE_COUNT))
        RecoveryCode.objects.bulk_create(
            RecoveryCode(
                batch=batch, code_hash=_recovery_hash(user_id=user_id, code=code)
            )
            for code in codes
        )
        record_phase1_event(
            "identity.recovery.enrolled",
            clinic_id=clinic_id,
            affected_record_id=batch.pk,
        )
    return OnceShownRecoveryCodes(batch_id=batch.pk, codes=codes)


def recover_authenticator(*, clinic_id: UUID, code: str) -> bool:
    """Consume exactly one own digest and retire old devices/codes/sessions."""
    normalized = code.strip().casefold()
    if RECOVERY_CODE_PATTERN.fullmatch(normalized) is None:
        return False
    user_id = current_actor_id()
    require_current_actor_clinic_roles(
        clinic_id=clinic_id, roles=tuple(UserClinicRole.Role)
    )
    digest = _recovery_hash(user_id=user_id, code=normalized)
    with transaction.atomic(), connection.cursor() as cursor:
        selected = RecoveryCode.objects.filter(
            code_hash=digest, batch__user_id=user_id, used_at__isnull=True
        ).first()
        if selected is None:
            return False
        cursor.execute("SELECT clinic_app.identity_recovery_consume(%s)", [digest])
        if cursor.fetchone() != (True,):
            return False
        record_phase1_event(
            "identity.recovery.consumed",
            clinic_id=clinic_id,
            affected_record_id=selected.pk,
        )
    return True


def request_authenticator_reset(
    *, clinic_id: UUID, user_id: UUID, session_key: str
) -> AuthenticatorReset:
    """Queue a scoped notification; provider acceptance starts no delay."""
    actor_id, clinic = require_staff_management(clinic_id=clinic_id)
    require_session_step_up(clinic_id=clinic_id, session_key=session_key)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.identity_reset_target(%s, %s)", [clinic_id, user_id]
        )
        if cursor.fetchone() != (True,):
            raise LifecycleDeniedError
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [f"authenticator-reset:{clinic_id}:{user_id}"],
        )
        existing = AuthenticatorReset.objects.filter(
            clinic_id=clinic_id, user_id=user_id, executed_at__isnull=True
        ).first()
        if existing is not None:
            return existing
        reset = AuthenticatorReset.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            user_id=user_id,
            requested_by_id=actor_id,
        )
        reset.notification_operation_id = enqueue_operation(
            request=OperationRequest(
                channel="email",
                provider=EMAIL_PROVIDER,
                clinic_id=clinic_id,
                subject_type=RESET_SUBJECT,
                subject_id=reset.pk,
                idempotency_key=reset.pk,
            )
        )
        reset.save(update_fields=("notification_operation",))
        record_phase1_event(
            "identity.authenticator.reset_requested",
            clinic_id=clinic_id,
            affected_record_id=reset.pk,
        )
    return reset


def execute_authenticator_reset(
    *, clinic_id: UUID, reset_id: UUID, session_key: str
) -> None:
    """Require actual notification, full DB24h and current exact-clinic step-up."""
    require_staff_management(clinic_id=clinic_id)
    require_session_step_up(clinic_id=clinic_id, session_key=session_key)
    with transaction.atomic(), connection.cursor() as cursor:
        reset = AuthenticatorReset.objects.filter(
            pk=reset_id, clinic_id=clinic_id
        ).first()
        if reset is None:
            raise LifecycleDeniedError
        cursor.execute(
            "SELECT clinic_app.identity_reset_execute(%s, %s)",
            [reset_id, session_hash(session_key)],
        )
        if cursor.fetchone() != (True,):
            raise LifecycleDeniedError
        record_phase1_event(
            "identity.authenticator.reset_executed",
            clinic_id=clinic_id,
            affected_record_id=reset_id,
        )
