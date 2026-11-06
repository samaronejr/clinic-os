"""Reuse encrypted intake registration, with an exact-ANS nullable backfill."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import connection

from apps.audit.services import record_phase1_event
from apps.insurance.access import clinic_write
from apps.insurance.errors import InsuranceDeniedError
from apps.insurance.models import Payer
from apps.intake.models import InsuranceMembership as RegisteredMembership
from apps.intake.models import PatientClinicEnrollment
from apps.tenancy.envelope import _kek

if TYPE_CHECKING:
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class PayerBackfill:
    """Only record identifiers, never patient names, cards or plaintext values."""

    linked: tuple[UUID, ...]
    already_linked: tuple[UUID, ...]
    unmatched: tuple[UUID, ...]
    ambiguous: tuple[UUID, ...]


def payer_match(ans: str, candidates: dict[str, tuple[UUID, ...]]) -> UUID | None:
    """No name, fuzzy, padding or first-row fallback can select a payer."""
    if re.fullmatch(r"[0-9]{6}", ans, flags=re.ASCII) is None:
        return None
    matches = candidates.get(ans, ())
    return matches[0] if len(matches) == 1 else None


def backfill_membership_payers(*, clinic_id: UUID) -> PayerBackfill:
    """Link only exact, unique local catalog matches; retain and report nulls."""
    with clinic_write(clinic_id=clinic_id) as clinic:
        grouped: dict[str, tuple[UUID, ...]] = {}
        for payer in Payer.objects.filter(clinic_id=clinic_id):
            grouped[payer.ans_registry] = (
                *grouped.get(payer.ans_registry, ()),
                payer.pk,
            )
        patients = PatientClinicEnrollment.objects.filter(
            clinic_id=clinic_id, organization_id=clinic.organization_id
        ).values_list("patient_id", flat=True)
        rows = RegisteredMembership.objects.filter(
            organization_id=clinic.organization_id, patient_id__in=patients
        ).only("id", "organization_id", "patient_id", "payer_id", "ans_number")
        linked: list[UUID] = []
        existing: list[UUID] = []
        unmatched: list[UUID] = []
        ambiguous: list[UUID] = []
        for row in rows.order_by("id"):
            if row.payer_id is not None:
                existing.append(row.pk)
                continue
            ans = row.ans_number or ""
            selected = payer_match(ans, grouped)
            if selected is None:
                (ambiguous if len(grouped.get(ans, ())) > 1 else unmatched).append(
                    row.pk
                )
                continue
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT clinic_app.insurance_link_registered_payer(%s,%s,%s,%s)",
                    [clinic_id, row.pk, selected, _kek()],
                )
                result = cursor.fetchone()
            if result is not None and result[0] is True:
                linked.append(row.pk)
                record_phase1_event(
                    "insurance.registration.payer_linked",
                    clinic_id=clinic_id,
                    affected_record_id=row.pk,
                )
            else:
                row.refresh_from_db(fields=("payer_id",))
                (existing if row.payer_id is not None else unmatched).append(row.pk)
        return PayerBackfill(
            tuple(linked), tuple(existing), tuple(unmatched), tuple(ambiguous)
        )


def registered_membership(
    *, clinic_id: UUID, enrollment_id: UUID, registration_id: UUID
) -> RegisteredMembership:
    """Load only a registration belonging to the selected enrollment's patient."""
    try:
        enrollment = PatientClinicEnrollment.objects.get(
            pk=enrollment_id, clinic_id=clinic_id
        )
        return RegisteredMembership.objects.get(
            pk=registration_id,
            organization_id=enrollment.organization_id,
            patient_id=enrollment.patient_id,
        )
    except (
        PatientClinicEnrollment.DoesNotExist,
        RegisteredMembership.DoesNotExist,
    ) as error:
        raise InsuranceDeniedError from error
