"""Commit-bound domain hints and immediate authorization invalidation triggers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.contrib.auth.signals import user_logged_out
from django.contrib.sessions.models import Session
from django.db.models.signals import post_delete, post_save, pre_save
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.ehr.models import Encounter
from apps.identity.models import (
    CareTeamMembership,
    PhysicianProfile,
    ProfessionalRegistration,
    RoleGrant,
    User,
    UserClinicRole,
)
from apps.intake.models import (
    PatientAccessGrant,
    PatientClinicEnrollment,
    PatientSession,
)
from apps.intake.patient_access import PATIENT_SESSION_KEY
from apps.realtime.transport import publish_on_commit
from apps.scheduling.models import Appointment
from apps.scheduling.patient_authority import patient_booking_scope

if TYPE_CHECKING:
    from uuid import UUID

    from django.db.models import Model
    from django.http import HttpRequest


def _invalidate(user_id: UUID | str) -> None:
    publish_on_commit(topic=f"authz:user:{user_id}", kind="revoked", version=1)


def appointment_changed(
    sender: type[Appointment], instance: Appointment, **kwargs: object
) -> None:
    """Booking, moves and status changes invalidate agenda and reception queue."""
    del sender, kwargs
    if not settings.REALTIME_ENABLED:
        return
    for kind in ("agenda", "queue"):
        publish_on_commit(
            topic=f"clinic:{instance.clinic_id}:{kind}", kind=kind, version=1
        )
    scope = patient_booking_scope()
    enrollment = (
        scope.enrollment_id
        if scope
        else PatientClinicEnrollment.objects.filter(
            clinic_id=instance.clinic_id, patient_id=instance.patient_id
        )
        .values_list("pk", flat=True)
        .first()
    )
    if enrollment is not None:
        publish_on_commit(
            topic=f"patient:{enrollment}:booking", kind="agenda", version=1
        )


def remember_membership(
    sender: type[UserClinicRole], instance: UserClinicRole, **kwargs: object
) -> None:
    """Retain the old subject for owner-side membership reassignment."""
    del kwargs
    if settings.REALTIME_ENABLED:
        previous = (
            sender.objects.filter(pk=instance.pk)
            .values_list("user_id", flat=True)
            .first()
        )
        instance.__dict__["realtime_previous_user"] = previous


def role_revoked(
    sender: type[Model],
    instance: UserClinicRole
    | CareTeamMembership
    | ProfessionalRegistration
    | PhysicianProfile
    | TOTPDevice,
    **kwargs: object,
) -> None:
    """Membership and subject-scope saves/deletes invalidate old and new subjects."""
    del sender, kwargs
    _invalidate(instance.user_id)
    previous = instance.__dict__.get("realtime_previous_user")
    if previous is not None and previous != instance.user_id:
        _invalidate(previous)


def permission_changed(
    sender: type[RoleGrant], instance: RoleGrant, **kwargs: object
) -> None:
    """Invalidate every affected clinic member after a role subtraction commits."""
    del sender, kwargs
    if settings.REALTIME_ENABLED:
        for user_id in (
            UserClinicRole.objects.filter(
                clinic_id=instance.clinic_id, role=instance.role
            )
            .values_list("user_id", flat=True)
            .distinct()
        ):
            _invalidate(user_id)


def user_changed(sender: type[User], instance: User, **kwargs: object) -> None:
    """Active state and password changes invalidate all of the user's sessions."""
    del sender, kwargs
    _invalidate(instance.pk)


def session_changed(sender: type[Session], instance: Session, **kwargs: object) -> None:
    """Session saves (including expiry) and deletes invalidate their bound subject."""
    del sender, kwargs
    if settings.REALTIME_ENABLED:
        data = instance.get_decoded()
        if subject := data.get(PATIENT_SESSION_KEY) or data.get(SESSION_KEY):
            _invalidate(subject)


def patient_session_changed(
    sender: type[PatientSession], instance: PatientSession, **kwargs: object
) -> None:
    """Patient authority uses the patient-session subject, never a staff user."""
    del sender, kwargs
    _invalidate(instance.pk)


def patient_grant_changed(
    sender: type[PatientAccessGrant], instance: PatientAccessGrant, **kwargs: object
) -> None:
    """Access revocation bulk-updates sessions; invalidate all of them on commit."""
    del sender, kwargs
    if settings.REALTIME_ENABLED:
        for session_id in PatientSession.objects.filter(
            grant_id=instance.pk
        ).values_list("pk", flat=True):
            _invalidate(session_id)


def encounter_changed(
    sender: type[Encounter], instance: Encounter, **kwargs: object
) -> None:
    """Recheck clinical job authority when its assigned encounter changes."""
    del sender, kwargs
    _invalidate(instance.physician_id)


def logged_out(
    sender: type[User], request: HttpRequest, user: User | None, **kwargs: object
) -> None:
    """Logout invalidates existing streams, not just subsequent HTTP refetches."""
    del sender, request, kwargs
    if user is not None:
        _invalidate(user.pk)


def connect_hooks() -> None:
    """Keep AppConfig re-entry idempotent; every publisher uses on_commit."""
    post_save.connect(
        appointment_changed, sender=Appointment, dispatch_uid="realtime.appointment"
    )
    pre_save.connect(
        remember_membership,
        sender=UserClinicRole,
        dispatch_uid="realtime.membership.previous",
    )
    for signal in (post_save, post_delete):
        for model in (
            UserClinicRole,
            CareTeamMembership,
            ProfessionalRegistration,
            PhysicianProfile,
            TOTPDevice,
        ):
            signal.connect(
                role_revoked,
                sender=model,
                dispatch_uid="realtime.subject." + model.__name__,
            )
        for model, receiver in (
            (RoleGrant, permission_changed),
            (User, user_changed),
            (Session, session_changed),
            (PatientSession, patient_session_changed),
            (PatientAccessGrant, patient_grant_changed),
            (Encounter, encounter_changed),
        ):
            signal.connect(
                receiver,
                sender=model,
                dispatch_uid="realtime.authority." + model.__name__,
            )
    user_logged_out.connect(logged_out, dispatch_uid="realtime.logout")
