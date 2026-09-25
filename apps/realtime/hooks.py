"""Schedule transport invalidation from real domain transitions, never precommit."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.signals import user_logged_out
from django.db.models.signals import post_delete, post_save

from apps.identity.models import UserClinicRole
from apps.realtime.transport import publish_on_commit
from apps.scheduling.models import Appointment

if TYPE_CHECKING:
    from django.http import HttpRequest

    from apps.identity.models import User


def appointment_changed(
    sender: type[Appointment], instance: Appointment, **kwargs: object
) -> None:
    """All staff/patient booking paths share the same model save boundary."""
    del sender, kwargs
    publish_on_commit(f"clinic:{instance.clinic_id}:agenda", "agenda", 1)


def role_revoked(
    sender: type[UserClinicRole], instance: UserClinicRole, **kwargs: object
) -> None:
    """Close every session for this user, even if another clinic role remains."""
    del sender, kwargs
    publish_on_commit(f"authz:user:{instance.user_id}", "revoked", 1)


def logged_out(
    sender: type[User], request: HttpRequest, user: User | None, **kwargs: object
) -> None:
    """Logout closes existing streams; session reload also catches missed events."""
    del sender, request, kwargs
    if user is not None:
        publish_on_commit(f"authz:user:{user.pk}", "revoked", 1)


def connect_hooks() -> None:
    """Keep AppConfig re-entry idempotent."""
    post_save.connect(
        appointment_changed, sender=Appointment, dispatch_uid="realtime.appointment"
    )
    post_delete.connect(
        role_revoked, sender=UserClinicRole, dispatch_uid="realtime.role"
    )
    user_logged_out.connect(logged_out, dispatch_uid="realtime.logout")
