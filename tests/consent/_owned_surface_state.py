"""Private real-role observations for the task-owned interactive fixture."""

from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING

import psycopg
from apps.consent.models import (
    ConsentAcceptance,
    ConsentPurpose,
    ConsentText,
    ParticipantAcknowledgment,
    RefusalRecord,
)
from apps.consent.services import consent_for_future_use
from apps.ehr.models import ClinicalDocumentVersion
from apps.intake.models import Patient
from apps.tenancy.db import tenant_context
from django.db import IntegrityError, transaction

from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from consent.test_record_scope import ScopeWorld


def _forged_text(world: ScopeWorld) -> dict[str, object]:
    text = "Texto sintético para a verificação da taxonomia."
    with runtime_role(), tenant_context(world.graph.clinic_admin, world.organization):
        before = ConsentText.objects.count()
        try:
            with transaction.atomic():
                ConsentText.objects.create(
                    organization_id=world.organization,
                    clinic_id=world.clinic_a,
                    purpose="recording",
                    version=1,
                    language="pt-BR",
                    text=text,
                    digest=sha256(text.encode()).hexdigest(),
                    published_by_id=world.graph.clinic_admin,
                )
        except IntegrityError as error:
            cause = error.__cause__
            assert isinstance(cause, psycopg.errors.CheckViolation)
            assert cause.sqlstate == "23514"
            assert cause.diag.constraint_name == "consent_text_scope_check"
            assert ConsentText.objects.count() == before
            return {
                "exception": type(cause).__name__,
                "sqlstate": cause.sqlstate,
                "constraint": cause.diag.constraint_name,
                "primary": cause.diag.message_primary,
                "rows_unchanged": True,
            }
        message = "Forged purpose unexpectedly inserted"
        raise AssertionError(message)


def observe_surface(world: ScopeWorld, command: str) -> dict[str, object]:
    """Inspect retained rows and execute the existing purpose gate as a reader."""
    if command == "CHECK":
        return _forged_text(world)
    assert command == "SNAPSHOT"
    with owner_context(world.organization):
        state: dict[str, object] = {
            "patients": Patient.objects.count(),
            "acknowledgments": ParticipantAcknowledgment.objects.filter(
                clinic_id=world.clinic_a
            ).count(),
            "acceptances": [
                [purpose, version, revocation is None]
                for purpose, version, revocation in ConsentAcceptance.objects.filter(
                    enrollment_id=world.enrollment_a
                ).values_list("text__purpose", "text__version", "revocation__pk")
            ],
            "refusals": list(
                RefusalRecord.objects.filter(
                    enrollment_id=world.enrollment_a
                ).values_list("text__purpose", "text__version")
            ),
            "notes": list(
                ClinicalDocumentVersion.objects.filter(
                    document__encounter_id=world.encounter_a
                ).values("revision", "state")
            ),
        }
    with runtime_role(), tenant_context(world.reader, world.organization):
        state["future"] = {
            purpose: consent_for_future_use(
                clinic_id=world.clinic_a,
                enrollment_id=world.enrollment_a,
                purpose=purpose,
            )
            is not None
            for purpose in ConsentPurpose.values
        }
    return state
