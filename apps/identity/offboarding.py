"""Atomic offboarding with an initially empty, startup-only handler registry."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING
from uuid import UUID

from django.apps import apps
from django.db import connection, transaction

from apps.audit.services import record_phase1_event
from apps.identity.lifecycle_authority import (
    LifecycleDeniedError,
    require_staff_management,
)
from apps.identity.offboarding_registry import (
    register_offboarding_handler as _register_offboarding_handler,
)
from apps.identity.offboarding_registry import registered_offboarding_handlers
from apps.scheduling.locks import (
    acquire_advisory_locks,
    clinic_lock_key,
    user_lock_keys,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from apps.identity.offboarding_registry import OffboardingHandler


class OffboardingRegistrationError(ValueError):
    """A handler must be unique, synchronous and registered during startup."""


class OffboardingHandlerError(TypeError):
    """A domain handler must return None within the caller's transaction."""


def register_offboarding_handler(handler: OffboardingHandler) -> None:
    """Register no default handler; task26 supplies its own transfer at startup."""
    if apps.ready:
        raise OffboardingRegistrationError
    _register_offboarding_handler(handler)


def offboard_staff(*, clinic_id: UUID, user_id: UUID) -> None:
    """Commit all handlers, future-work checks and retained revocations together."""
    _, clinic = require_staff_management(clinic_id=clinic_id)
    with transaction.atomic(), connection.cursor() as cursor:
        acquire_advisory_locks(
            (f"clinic-lock-v1:identity-owner:{clinic.organization_id}",)
        )
        cursor.execute(
            "SELECT clinic_app.identity_offboarding_clinics(%s, %s)",
            [user_id, clinic_id],
        )
        result = cursor.fetchone()
        if result is None or result[0] is None:
            raise LifecycleDeniedError
        clinic_ids = tuple(UUID(str(value)) for value in result[0])
        acquire_advisory_locks(tuple(clinic_lock_key(value) for value in clinic_ids))
        acquire_advisory_locks(user_lock_keys((user_id,)))
        for selected_clinic in clinic_ids:
            for handler in registered_offboarding_handlers():
                # Widen only at the runtime contract check, not in the registry.
                runtime_handler: Callable[..., None | bool] = handler
                result = runtime_handler(clinic_id=selected_clinic, user_id=user_id)
                if inspect.iscoroutine(result):
                    result.close()
                    raise OffboardingHandlerError
                if result is not None:
                    raise OffboardingHandlerError
        cursor.execute(
            "SELECT clinic_app.identity_offboarding_revoke(%s, %s)",
            [user_id, clinic_id],
        )
        if cursor.fetchone() != (True,):
            raise LifecycleDeniedError
        record_phase1_event(
            "identity.staff.offboarded",
            clinic_id=clinic_id,
            affected_record_id=user_id,
        )
