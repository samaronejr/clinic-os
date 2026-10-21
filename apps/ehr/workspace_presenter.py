"""Read the patient workspace through current scoped policies."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.utils import timezone
from django.utils.translation import gettext as _

from apps.ehr.longitudinal_access import patient_scope
from apps.ehr.longitudinal_values import CODES
from apps.ehr.models import (
    ClinicalDocumentVersion,
    Encounter,
    EncounterAddendum,
    Episode,
    MedicationStatement,
    Observation,
)
from apps.ehr.note_models import CopiedNoteBlock, RestrictedNote
from apps.ehr.services import ClinicalAccessDeniedError
from apps.ehr.timeline import TIMELINE_KINDS, TimelineFilters, timeline
from apps.ehr.workspace_forms import MedicationForm, ObservationForm, VitalsForm
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import Clinic
from apps.intake.models import PatientClinicEnrollment

if TYPE_CHECKING:
    from django.forms import Form

    from apps.ehr.longitudinal_access import PatientScope

TABS = (
    ("overview", _("Overview"), "o", True),
    ("timeline", _("Timeline"), "t", True),
    ("encounters", _("Encounters"), "e", True),
    ("medications", _("Medications"), "m", True),
    ("results", _("Results"), "r", False),
    ("documents", _("Documents"), "d", True),
    ("plans", _("Plans"), "p", False),
    ("forms", _("Forms"), "f", True),
    ("messages", _("Messages"), "c", False),
    ("vitals", _("Vitals and observations"), "v", True),
)


def _vitals_context(
    *, scope: PatientScope, zone: ZoneInfo, trend_kind: str
) -> dict[str, object]:
    """Build one unit-consistent chart and table from the same authorized values."""
    context: dict[str, object] = {}
    observations = list(
        Observation.objects.filter(
            clinic_id=scope.clinic_id, patient_id=scope.patient_id
        ).order_by("-effective_at", "-created_at")[:200]
    )
    context["observations"] = observations
    if trend_kind not in CODES or CODES[trend_kind].choices:
        trend_kind = "temperature"
    series = list(reversed([o for o in observations if o.kind == trend_kind][:25]))
    context.update(
        trend_kind=trend_kind,
        trend_choices=[
            (key, code.label) for key, code in CODES.items() if not code.choices
        ],
        chart_labels=[
            timezone.localtime(o.effective_at, zone).strftime("%d/%m/%Y %H:%M")
            for o in series
        ],
        chart_series=[
            {
                "name": str(CODES[trend_kind].label),
                "values": [o.value["value"] for o in series],
            }
        ],
        chart_title=f"{CODES[trend_kind].label} ({CODES[trend_kind].unit})",
        chart_state="default" if series else "empty",
    )
    return context


def _notes_context(*, scope: PatientScope) -> dict[str, object]:
    """List readable source notes and the current author's retained copy receipts."""
    context: dict[str, object] = {}
    versions = list(
        ClinicalDocumentVersion.objects.filter(
            document__encounter__clinic_id=scope.clinic_id,
            document__encounter__patient_id=scope.patient_id,
        )
        .select_related("document__encounter", "template")
        .order_by("-created_at")[:100]
    )
    context["notes"] = [
        {
            "version": v,
            "owned": v.author_id == scope.user_id,
            "restricted": RestrictedNote.objects.filter(
                document_id=v.document_id
            ).exists(),
            "copies": CopiedNoteBlock.objects.filter(target=v),
        }
        for v in versions
    ]
    context["drafts"] = [
        v for v in versions if v.state == "draft" and v.author_id == scope.user_id
    ]
    context["encounters"] = (
        Encounter.objects.filter(
            clinic_id=scope.clinic_id, patient_id=scope.patient_id, state="open"
        )
        .exclude(physician_id=scope.user_id)
        .order_by("-created_at")[:50]
    )
    context["addenda"] = EncounterAddendum.objects.filter(
        clinic_id=scope.clinic_id,
        patient_id=scope.patient_id,
        author_id=scope.user_id,
    ).order_by("-created_at")[:50]
    context["patient_encounters"] = Encounter.objects.filter(
        clinic_id=scope.clinic_id, patient_id=scope.patient_id
    ).order_by("-created_at")[:100]
    return context


def _editing_permissions(*, scope: PatientScope) -> dict[str, bool]:
    """Describe editable controls without replacing any domain authorization."""
    permissions: dict[str, bool] = {}
    for action in ("write", "observe"):
        try:
            patient_scope(
                clinic_id=scope.clinic_id,
                enrollment_id=scope.enrollment_id,
                action=action,
            )
            permissions[action] = True
        except ClinicalAccessDeniedError:
            permissions[action] = False
    return permissions


def workspace_context(
    *,
    scope: PatientScope,
    tab: str,
    filters: TimelineFilters,
    forms: dict[str, Form],
    trend_kind: str,
) -> dict[str, object]:
    """Materialize bounded authorized rows, with a chart/table from the same values."""
    clinic = Clinic.objects.get(pk=scope.clinic_id)
    zone_name = clinic.timezone
    if zone_name is None:
        raise ClinicalAccessDeniedError
    enrollment = PatientClinicEnrollment.objects.select_related("patient").get(
        pk=scope.enrollment_id, clinic_id=scope.clinic_id
    )
    try:
        require_permission(
            "clinical.read",
            clinic_id=scope.clinic_id,
            patient_enrollment_id=scope.enrollment_id,
        )
        narrative = True
    except CurrentActorError:
        narrative = False
    if tab not in {"overview", "vitals", "timeline"} and not narrative:
        raise ClinicalAccessDeniedError
    permissions = _editing_permissions(scope=scope)
    context: dict[str, object] = {
        "clinic_id": scope.clinic_id,
        "clinic": clinic,
        "enrollment": enrollment,
        "tab": tab,
        "tabs": [
            {"key": key, "label": label, "shortcut": shortcut, "available": available}
            for key, label, shortcut, available in TABS
            if narrative or key in {"overview", "vitals", "timeline"}
        ],
        "can_write": permissions["write"],
        "can_narrative": narrative,
        "can_observe": permissions["observe"],
        "forms": forms,
        "command_id": uuid4(),
        "render_id": uuid4(),
        "filters": filters,
        "timeline_kinds": TIMELINE_KINDS,
    }
    context["page"] = timeline(
        clinic_id=scope.clinic_id, enrollment_id=scope.enrollment_id, filters=filters
    )
    if tab in {"overview", "vitals"}:
        context.update(
            _vitals_context(
                scope=scope, zone=ZoneInfo(zone_name), trend_kind=trend_kind
            )
        )
    if tab == "medications":
        context["medications"] = MedicationStatement.objects.filter(
            clinic_id=scope.clinic_id, patient_id=scope.patient_id
        ).order_by("-effective_at", "-pk")[:100]
    if narrative:
        context["episodes"] = Episode.objects.filter(
            clinic_id=scope.clinic_id, patient_id=scope.patient_id
        ).order_by("-opened_at")[:100]
    if tab == "encounters":
        context.update(_notes_context(scope=scope))
    if tab == "documents":
        context["document_encounters"] = Encounter.objects.filter(
            clinic_id=scope.clinic_id, patient_id=scope.patient_id
        ).order_by("-created_at")[:100]
    return context


def empty_forms() -> dict[str, Form]:
    """Give each native form its own field namespace."""
    return {
        "vitals": VitalsForm(prefix="vitals"),
        "observation": ObservationForm(prefix="observation"),
        "medication": MedicationForm(prefix="medication"),
    }
