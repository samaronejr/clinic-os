"""Owner-controlled clinic timezone mutation."""

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.audit.services import record_phase1_event
from apps.identity.management.base import LifecycleCommandError
from apps.identity.management.context import (
    LifecycleContext,
    assume_runtime_owner,
    scoped_owner_gucs,
)
from apps.identity.models import Clinic
from apps.scheduling.locks import acquire_advisory_locks, clinic_lock_key
from apps.scheduling.timezones import (
    ClinicTimezoneLockedError,
    ClinicTimezoneStateError,
    ensure_clinic_timezone_change_allowed,
    validate_iana_timezone,
)


def set_clinic_timezone(context: LifecycleContext, timezone_key: str) -> None:
    """Change one empty clinic timezone and append its exact tenant event."""
    with transaction.atomic(), scoped_owner_gucs(context):
        acquire_advisory_locks((clinic_lock_key(context.clinic_id),))
        with assume_runtime_owner(context):
            pass
        try:
            validate_iana_timezone(timezone_key)
        except ValidationError as error:
            raise LifecycleCommandError from error
        clinic = Clinic.objects.filter(
            pk=context.clinic_id,
            organization_id=context.organization_id,
        ).first()
        if clinic is None:
            raise LifecycleCommandError
        if clinic.timezone == timezone_key:
            return
        try:
            ensure_clinic_timezone_change_allowed(clinic.pk)
        except (ClinicTimezoneLockedError, ClinicTimezoneStateError) as error:
            raise LifecycleCommandError from error
        clinic.timezone = timezone_key
        clinic.save(update_fields=("timezone",))
        record_phase1_event(
            "identity.clinic_timezone.changed",
            clinic_id=clinic.pk,
            affected_record_id=clinic.pk,
        )
