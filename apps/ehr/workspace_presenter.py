"""Read the patient workspace through current scoped policies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.utils import timezone
from django.utils.translation import gettext as _

from apps.comms.inbox_queries import STATES as CONVERSATION_STATES
from apps.comms.inbox_queries import list_conversations
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
from apps.ehr.pack_models import SpecialtyEntry, SpecialtyPack
from apps.ehr.services import ClinicalAccessDeniedError
from apps.ehr.specialty_forms import specialty_forms
from apps.ehr.timeline import TIMELINE_KINDS, TimelineFilters, timeline
from apps.ehr.workspace_forms import MedicationForm, ObservationForm, VitalsForm
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import Clinic
from apps.intake.models import PatientClinicEnrollment
from apps.orders.queries import result_rows
from apps.orders.values import OrdersAccessDeniedError
from apps.workflows.care.models import CarePlan
from apps.workflows.care.vocab import (
    ACTION_KIND_LABELS,
    ACTION_STATE_LABELS,
    PLAN_STATE_LABELS,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.forms import Form

    from apps.ehr.longitudinal_access import PatientScope

TABS = (
    ("overview", _("Overview"), "o"),
    ("timeline", _("Timeline"), "t"),
    ("encounters", _("Encounters"), "e"),
    ("medications", _("Medications"), "m"),
    ("results", _("Results"), "r"),
    ("documents", _("Documents"), "d"),
    ("plans", _("Plans"), "p"),
    ("forms", _("Forms"), "f"),
    ("messages", _("Messages"), "c"),
    ("vitals", _("Vitals and observations"), "v"),
    ("specialty", _("Specialty records"), "s"),
)
# Results, plans and conversations are owned by Results, care coordination and
# Messages; the workspace reads them through those features' own scoped queries.
CARE_PLAN_LIMIT = 20


def _results_context(*, scope: PatientScope) -> dict[str, object]:
    """List current filed results; a refused reader gets the restricted notice."""
    try:
        rows = result_rows(clinic_id=scope.clinic_id, enrollment_id=scope.enrollment_id)
    except OrdersAccessDeniedError:
        return {"section_restricted": True}
    return {"results": rows}


def _plans_context(*, scope: PatientScope) -> dict[str, object]:
    """Care plans with their actions, labelled from the closed vocabularies."""
    plans = CarePlan.objects.filter(
        clinic_id=scope.clinic_id, enrollment_id=scope.enrollment_id
    ).order_by("-created_at")[:CARE_PLAN_LIMIT]
    return {
        "care_plans": [
            {
                "plan": plan,
                "state_label": PLAN_STATE_LABELS[plan.state],
                "actions": [
                    {
                        "action": action,
                        "kind_label": _(ACTION_KIND_LABELS[action.kind]),
                        "state_label": ACTION_STATE_LABELS[action.state],
                    }
                    for action in plan.actions.order_by("window_start", "pk")
                ],
            }
            for plan in plans
        ]
    }


def _messages_context(*, scope: PatientScope) -> dict[str, object]:
    """List the patient's conversations the actor may open, folder by folder."""
    try:
        rows = [
            row
            for state in CONVERSATION_STATES
            for row in list_conversations(
                clinic_id=scope.clinic_id,
                state=state,
                enrollment_id=scope.enrollment_id,
            )
        ]
    except CurrentActorError:
        return {"section_restricted": True}
    return {"conversations": rows}


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


def _medications_context(*, scope: PatientScope) -> dict[str, object]:
    """List reported and reconciled medication statements, newest first."""
    return {
        "medications": MedicationStatement.objects.filter(
            clinic_id=scope.clinic_id, patient_id=scope.patient_id
        ).order_by("-effective_at", "-pk")[:100]
    }


def _documents_context(*, scope: PatientScope) -> dict[str, object]:
    """List encounters whose documents the reader may open."""
    return {
        "document_encounters": Encounter.objects.filter(
            clinic_id=scope.clinic_id, patient_id=scope.patient_id
        ).order_by("-created_at")[:100]
    }


# Read-only sections: each tab key loads only its own rows.
SECTION_CONTEXTS: Final[dict[str, Callable[..., dict[str, object]]]] = {
    "medications": _medications_context,
    "encounters": _notes_context,
    "results": _results_context,
    "documents": _documents_context,
    "plans": _plans_context,
    "messages": _messages_context,
}


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
            {"key": key, "label": label, "shortcut": shortcut}
            for key, label, shortcut in TABS
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
    if tab in SECTION_CONTEXTS:
        context.update(SECTION_CONTEXTS[tab](scope=scope))
    if narrative:
        context["episodes"] = Episode.objects.filter(
            clinic_id=scope.clinic_id, patient_id=scope.patient_id
        ).order_by("-opened_at")[:100]
    if tab == "specialty":
        forms.update(
            {
                name: forms.get(name) or form
                for name, form in specialty_forms(scope=scope).items()
            }
        )
        context["specialty_packs"] = SpecialtyPack.objects.filter(
            clinic_id=scope.clinic_id, uninstalled_at__isnull=True
        ).order_by("position", "key")
        context["specialty_entries"] = SpecialtyEntry.objects.filter(
            clinic_id=scope.clinic_id, encounter__patient_id=scope.patient_id
        ).order_by("-created_at")[:100]
    return context


def empty_forms() -> dict[str, Form]:
    """Give each native form its own field namespace."""
    return {
        "vitals": VitalsForm(prefix="vitals"),
        "observation": ObservationForm(prefix="observation"),
        "medication": MedicationForm(prefix="medication"),
    }
