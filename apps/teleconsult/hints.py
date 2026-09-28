"""Commit-bound realtime refetch hints for one teleconsult session.

Two topics carry the same hint: ``teleconsult:<opaque room name>`` for the
assigned physician and ``patient:<enrollment>:teleconsult`` for the bound
patient. Events are todo 8's three-key invalidations; clients refetch the
session status and never read state from the hint itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from django.conf import settings

from apps.consent.services import patient_authority
from apps.intake.access import PatientAccessDeniedError
from apps.intake.models import PatientClinicEnrollment
from apps.realtime.transport import publish_on_commit
from apps.teleconsult.models import TeleconsultRoom
from apps.teleconsult.video_providers import ROOM_NAME

if TYPE_CHECKING:
    from apps.teleconsult.models import TeleconsultSession

KIND: Final = "teleconsult"
VERSION: Final = 1


def room_topic(room_name: str) -> str:
    """Return the physician's topic for one opaque room name."""
    return f"teleconsult:{room_name}"


def patient_topic(enrollment_id: object) -> str:
    """Return the bound patient's operation-scoped topic."""
    return f"patient:{enrollment_id}:teleconsult"


def publish_session_hint(session: TeleconsultSession) -> None:
    """Schedule both participants' refetch hints for after the commit.

    Legacy rooms (``tc-<uuid>``) have no physician topic; the grammar only
    admits opaque room names, so their physicians keep the explicit refresh.
    """
    if not settings.REALTIME_ENABLED:
        return
    room_name = (
        TeleconsultRoom.objects.filter(session_id=session.pk)
        .values_list("room_name", flat=True)
        .first()
    )
    if room_name is not None and ROOM_NAME.fullmatch(room_name):
        publish_on_commit(topic=room_topic(room_name), kind=KIND, version=VERSION)
    enrollment = (
        PatientClinicEnrollment.objects.filter(
            clinic_id=session.clinic_id, patient_id=session.patient_id
        )
        .values_list("pk", flat=True)
        .first()
    )
    if enrollment is None:
        # A patient principal cannot read enrollments; its own is bound.
        try:
            authority = patient_authority()
        except PatientAccessDeniedError:
            authority = None
        if authority is not None and authority.patient_id == session.patient_id:
            enrollment = authority.enrollment_id
    if enrollment is not None:
        publish_on_commit(topic=patient_topic(enrollment), kind=KIND, version=VERSION)
