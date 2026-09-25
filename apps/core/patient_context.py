"""The active patient context behind the shell's patient banner.

One patient per clinic may be in context. The context lives in the
server-side session (never a URL, title, cookie value or browser storage) and
is re-authorized on every render: a revoked grant hides the banner and drops
the context. Pages that display one patient's record (``PATIENT_BOUND_VIEWS``)
must bind that patient in the same request; when they do not, the banner is
suppressed rather than risk naming a different patient above the record.

Switching patients goes through registered context-switch guards (the hook
todo 27 attaches draft binding to and todo 40 attaches capture binding to).
A guard that reports bound work blocks the switch until the user explicitly
discards; the server never retargets bound work to the new patient.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final, Protocol
from uuid import UUID

from django.utils.translation import gettext, ngettext

from apps.core.navigation import PATIENT_ACTIONS, Destination, allows
from apps.ehr.history import read_history
from apps.ehr.models import Encounter
from apps.ehr.services import ClinicalAccessDeniedError
from apps.intake.models import PatientClinicEnrollment
from apps.scheduling.agenda_presenter import clinic_local_today

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

SESSION_PREFIX: Final = "workspace.patient."
BOUND_ATTRIBUTE: Final = "_clinic_workspace_bound_patient"
DEMOGRAPHICS_READ: Final = "demographics.read"
# Every staff view that renders one patient's record. A view listed here
# shows the banner only for the patient it bound in the same request.
PATIENT_BOUND_VIEWS: Final = frozenset(
    {
        "ehr:encounter",
        "ehr:history",
        "ehr:attachments",
        "intake:patient-contacts",
        "intake:patient-access",
        "intake:questionnaire-staff",
        "prescription:draft",
        "prescription:draft-encounter",
        "prescription:review",
        "prescription:signing",
        "teleconsult:staff",
    }
)


@dataclass(frozen=True, slots=True)
class ContextSwitch:
    """A requested change of the clinic's patient in context."""

    clinic_id: UUID
    from_enrollment_id: UUID | None
    to_enrollment_id: UUID | None


@dataclass(frozen=True, slots=True)
class SwitchConsequence:
    """Work bound to the current patient that a switch would abandon."""

    key: str
    message: str


class ContextSwitchGuard(Protocol):
    """Server-side hook consulted before the patient in context changes."""

    def check(self, switch: ContextSwitch) -> SwitchConsequence | None:
        """Return the consequence of switching, or ``None`` when nothing is bound."""

    def discard(self, switch: ContextSwitch) -> None:
        """Release the bound work after the user chose to discard it."""


_GUARDS: dict[str, ContextSwitchGuard] = {}


def register_context_switch_guard(key: str, guard: ContextSwitchGuard) -> None:
    """Attach one guard (draft binding, capture binding) by a stable key."""
    if key in _GUARDS:
        msg = f"context-switch guard already registered: {key}"
        raise ValueError(msg)
    _GUARDS[key] = guard


def unregister_context_switch_guard(key: str) -> None:
    """Detach a guard (tests and app teardown only)."""
    _GUARDS.pop(key, None)


def switch_consequences(switch: ContextSwitch) -> tuple[SwitchConsequence, ...]:
    """Ask every guard what the switch would abandon, in registration order."""
    found = (guard.check(switch) for guard in tuple(_GUARDS.values()))
    return tuple(consequence for consequence in found if consequence is not None)


def discard_bound_work(switch: ContextSwitch) -> None:
    """Let every guard release its bound work before the context changes."""
    for guard in tuple(_GUARDS.values()):
        if guard.check(switch) is not None:
            guard.discard(switch)


def _session_key(clinic_id: UUID) -> str:
    return f"{SESSION_PREFIX}{clinic_id}"


def _parse(raw: object) -> UUID | None:
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


def current_patient(request: HttpRequest, clinic_id: UUID) -> UUID | None:
    """Return the enrollment in the clinic's session context, if any."""
    return _parse(request.session.get(_session_key(clinic_id)))


def set_patient_context(
    request: HttpRequest, *, clinic_id: UUID, enrollment_id: UUID
) -> None:
    """Store one authorized enrollment as the clinic's patient in context."""
    request.session[_session_key(clinic_id)] = str(enrollment_id)


def clear_patient_context(request: HttpRequest, *, clinic_id: UUID) -> None:
    """Drop the clinic's patient context (close, revocation or stale record)."""
    request.session.pop(_session_key(clinic_id), None)


def bind_patient_context(
    request: HttpRequest,
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    encounter_id: UUID | None = None,
) -> None:
    """Record the patient a record page is rendering, after its own authorization.

    Called by patient-bound views once their service has authorized the read;
    the banner then names exactly this patient on this response, and the
    session context follows so the next page keeps it pinned.
    """
    setattr(request, BOUND_ATTRIBUTE, (clinic_id, enrollment_id, encounter_id))
    set_patient_context(request, clinic_id=clinic_id, enrollment_id=enrollment_id)


def enrollment_for_patient(*, clinic_id: UUID, patient_id: UUID) -> UUID | None:
    """Return the clinic enrollment of an organization patient, if any."""
    return (
        PatientClinicEnrollment.objects.filter(
            clinic_id=clinic_id, patient_id=patient_id
        )
        .values_list("pk", flat=True)
        .first()
    )


@dataclass(frozen=True, slots=True)
class MaskedIdentifier:
    """A document identifier shown masked (todo 17 supplies CPF/CNS)."""

    kind: str
    masked: str


@dataclass(frozen=True, slots=True)
class PatientBanner:
    """Minimum-necessary identity of the patient in context."""

    enrollment_id: UUID
    name: str
    social_name: str
    age_label: str
    identifiers: tuple[MaskedIdentifier, ...]
    allergy_state: str
    allergy_label: str
    active_encounter: bool
    delegate: bool
    restricted: bool
    actions: tuple[Destination, ...]
    bound: bool


def age_label(birth_date: date, today: date) -> str:
    """Return the completed years at the clinic-local date, in words."""
    years = today.year - birth_date.year
    if (today.month, today.day) < (birth_date.month, birth_date.day):
        years -= 1
    years = max(years, 0)
    return ngettext("%(age)s year", "%(age)s years", years) % {"age": years}


def _allergy_flag(clinic_id: UUID, encounter_id: UUID | None) -> tuple[str, str]:
    """Read the allergy assessment through the audited clinical history service.

    Only an encounter-bound banner (the assigned physician on the record)
    carries the flag; everyone else gets no allergy statement at all, so an
    unreadable history is never presented as an absence.
    """
    if encounter_id is None:
        return "", ""
    try:
        history = read_history(
            clinic_id=clinic_id, encounter_id=encounter_id, kind="allergy"
        )
    except ClinicalAccessDeniedError:
        return "", ""
    if history.state == "documented":
        active = sum(1 for entry in history.entries if entry.status == "active")
        return "documented", ngettext(
            "%(count)s active allergy", "%(count)s active allergies", active
        ) % {"count": active}
    if history.state == "none_documented":
        return "none_documented", gettext("No allergy documented")
    return "not_assessed", gettext("Allergies not assessed")


def _bound(request: HttpRequest, clinic_id: UUID) -> tuple[UUID, UUID | None] | None:
    bound = getattr(request, BOUND_ATTRIBUTE, None)
    if not isinstance(bound, tuple) or bound[0] != clinic_id:
        return None
    return bound[1], bound[2]


def resolve_patient_banner(  # noqa: PLR0913 - the banner needs the whole shell scope
    request: HttpRequest,
    *,
    clinic_id: UUID,
    timezone: str,
    roles: frozenset[str],
    granted: frozenset[str],
    view_name: str,
) -> PatientBanner | None:
    """Return the banner for this response, or ``None`` when no context applies."""
    bound = _bound(request, clinic_id)
    encounter_id: UUID | None = None
    if bound is not None:
        enrollment_id, encounter_id = bound
    elif view_name in PATIENT_BOUND_VIEWS:
        return None
    else:
        remembered = current_patient(request, clinic_id)
        if remembered is None:
            return None
        if DEMOGRAPHICS_READ not in granted:
            clear_patient_context(request, clinic_id=clinic_id)
            return None
        enrollment_id = remembered
    enrollment = (
        PatientClinicEnrollment.objects.select_related("patient")
        .filter(pk=enrollment_id, clinic_id=clinic_id)
        .first()
    )
    if enrollment is None:
        clear_patient_context(request, clinic_id=clinic_id)
        return None
    patient = enrollment.patient
    today = date.fromisoformat(clinic_local_today(timezone))
    allergy_state, allergy_text = _allergy_flag(clinic_id, encounter_id)
    active_encounter = (
        Encounter.objects.filter(
            pk=encounter_id, clinic_id=clinic_id, state=Encounter.State.OPEN
        ).exists()
        if encounter_id is not None
        else Encounter.objects.filter(
            clinic_id=clinic_id, patient_id=patient.pk, state=Encounter.State.OPEN
        ).exists()
    )
    return PatientBanner(
        enrollment_id=enrollment.pk,
        name=str(patient.full_name),
        social_name="",
        age_label=age_label(patient.birth_date, today),
        identifiers=(),
        allergy_state=allergy_state,
        allergy_label=allergy_text,
        active_encounter=active_encounter,
        delegate=False,
        restricted=False,
        actions=tuple(
            action
            for action in PATIENT_ACTIONS
            if allows(action, roles=roles, granted=granted)
        ),
        bound=bound is not None,
    )
