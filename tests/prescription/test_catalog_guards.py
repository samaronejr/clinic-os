"""Actual catalog role decisions and independently reached SQL binding guards."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ehr.services import open_encounter
from apps.identity.current_context import CurrentActorError
from apps.identity.models import User, UserClinicRole
from apps.intake.models import PatientClinicEnrollment
from apps.prescription.catalog import catalog_entries, favorite_entries, set_favorite
from apps.prescription.models import (
    MedicationCatalogEntry,
    MedicationCatalogImport,
    MedicationFavorite,
    PrescriptionDraft,
    SignatureOperation,
)
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction
from django.utils import timezone
from psycopg.errors import CheckViolation

from auth.stepup_test_support import create_role_actor
from patient_service_support import runtime_role
from renewal.test_encounters import setup_context
from renewal.test_signatures import setup
from scheduling.appointment_service_support import (
    AppointmentSetup,
    arrived_synthetic_appointment,
)

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph
    from renewal.test_signatures import SigningSetup

__all__ = ("setup",)
pytestmark = pytest.mark.django_db(transaction=True)


def catalog_entry() -> MedicationCatalogEntry:
    """Seed synthetic public terminology without assigning a clinical class."""
    source = MedicationCatalogImport.objects.create(
        source_url="https://www.gov.br/anvisa/pt-br/synthetic-authority.json",
        retrieved_on=date(2026, 10, 20),
        checksum=uuid4().hex * 2,
    )
    return MedicationCatalogEntry.objects.create(
        source=source,
        code="SYNTHETIC-AUTHORITY",
        description="Synthetic authority medicine",
        strength_form="Synthetic tablet",
    )


@pytest.mark.parametrize("role", UserClinicRole.Role.values)
@pytest.mark.parametrize("inactive", [False, True])
def test_catalog_boundaries_decide_every_role_and_active_state(
    rbac_graph: RbacGraph, role: str, *, inactive: bool
) -> None:
    entry = catalog_entry()
    actor = create_role_actor(rbac_graph, UserClinicRole.Role(role))
    if inactive:
        User.objects.filter(pk=actor.pk).update(is_active=False)
    for valid in (True, False):
        clinic = rbac_graph.clinic_a if valid else rbac_graph.clinic_b
        allowed = valid and not inactive and role == "physician"
        # Bind only GUCs so the actual callable, not tenant_context, decides.
        with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id',%s,true),"
                "set_config('app.current_tenant',%s,true)",
                [str(actor.pk), str(rbac_graph.organization_a)],
            )
            if allowed:
                catalog_entries(clinic_id=clinic, query="Synthetic")
                assert favorite_entries(clinic_id=clinic) == ()
                favorite = set_favorite(
                    clinic_id=clinic, entry_id=entry.pk, active=True
                )
                assert favorite.issuer_id == actor.pk
                assert [row.pk for row in favorite_entries(clinic_id=clinic)] == [
                    favorite.pk
                ]
            else:
                with pytest.raises(CurrentActorError):
                    catalog_entries(clinic_id=clinic, query="Synthetic")
                with pytest.raises(CurrentActorError):
                    favorite_entries(clinic_id=clinic)
                with pytest.raises(CurrentActorError):
                    set_favorite(clinic_id=clinic, entry_id=entry.pk, active=True)
            transaction.set_rollback(True)


@pytest.mark.parametrize("role", UserClinicRole.Role.values)
def test_favorite_trigger_binds_the_stored_physician_role(
    rbac_graph: RbacGraph, role: str
) -> None:
    entry = catalog_entry()
    actor = create_role_actor(rbac_graph, UserClinicRole.Role(role))
    with setup_context(rbac_graph.organization_a):
        if role == "physician":
            row = MedicationFavorite.objects.create(
                organization_id=rbac_graph.organization_a,
                clinic_id=rbac_graph.clinic_a,
                issuer=actor,
                entry=entry,
            )
            assert row.issuer_id == actor.pk
            UserClinicRole.objects.filter(user=actor).update(revoked_at=timezone.now())
        # The owner fixture bypasses the runtime role policy deliberately:
        # the exact trigger must reject a missing/revoked issuer role itself.
        with pytest.raises(DatabaseError) as refusal, transaction.atomic():
            MedicationFavorite.objects.create(
                organization_id=rbac_graph.organization_a,
                clinic_id=rbac_graph.clinic_a,
                issuer=actor,
                entry=entry,
            )
        cause = refusal.value.__cause__
        assert isinstance(cause, CheckViolation)
        context = cause.diag.context
        assert context is not None
        assert "prescription_favorite_guard" in context


def test_signature_class_trigger_rejects_rebinding(setup: SigningSetup) -> None:
    operation = setup.initiate()
    graph = setup.graph
    with setup_context(graph.organization_a):
        assert operation.category == "synthetic_non_controlled"
        failure = {
            "state": "failed",
            "completed_at": timezone.now(),
            "failure_reason": "synthetic-class-rebinding",
        }
        # A valid failure transition isolates class binding from the existing
        # state machine and from the runtime column ACL.
        with transaction.atomic():
            assert (
                SignatureOperation.objects.filter(pk=operation.pk).update(**failure)
                == 1
            )
            transaction.set_rollback(True)
        for category in (None, "simple"):
            with pytest.raises(DatabaseError) as refusal, transaction.atomic():
                SignatureOperation.objects.filter(pk=operation.pk).update(
                    category=category, **failure
                )
            cause = refusal.value.__cause__
            assert isinstance(cause, CheckViolation)
            context = cause.diag.context
            assert context is not None
            assert "prescription_signature_class_guard" in context
        operation.refresh_from_db()
        assert operation.category == "synthetic_non_controlled"


@pytest.mark.parametrize("role", UserClinicRole.Role.values)
def test_renewal_trigger_requires_source_care_for_every_role(
    setup: SigningSetup, role: str
) -> None:
    setup.issue()
    graph = setup.graph
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        enrollment = PatientClinicEnrollment.objects.get(
            patient_id=setup.document.patient_id, clinic_id=graph.clinic_a
        )
        appointment = arrived_synthetic_appointment(
            AppointmentSetup(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                actor_id=graph.shared_user,
                practitioner_id=graph.physician,
                enrollment_id=enrollment.pk,
                patient_id=setup.document.patient_id,
            ),
            start_local="2035-06-02T10:00",
            end_local="2035-06-02T11:00",
        )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        encounter = open_encounter(
            clinic_id=graph.clinic_a, appointment_id=appointment.pk
        )
    with setup_context(graph.organization_a), connection.cursor() as cursor:
        if role != "physician":
            UserClinicRole.objects.filter(
                user_id=graph.physician, clinic_id=graph.clinic_a
            ).update(revoked_at=timezone.now())
            UserClinicRole.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                user_id=graph.physician,
                role=role,
            )
        cursor.execute(
            "SELECT set_config('app.current_user_id',%s,true)",
            [str(graph.physician)],
        )
        fields = {
            "organization_id": graph.organization_a,
            "clinic_id": graph.clinic_a,
            "encounter_id": encounter.pk,
            "patient_id": setup.document.patient_id,
            "issuer_id": graph.physician,
            "renewal_source_id": setup.document.pk,
            "category": None,
            "contract_version": "prescription-classes-v1",
        }
        if role == "physician":
            draft = PrescriptionDraft.objects.create(**fields)
            assert draft.category is None
        else:
            with pytest.raises(DatabaseError) as refusal, transaction.atomic():
                PrescriptionDraft.objects.create(**fields)
            cause = refusal.value.__cause__
            assert isinstance(cause, CheckViolation)
            context = cause.diag.context
            assert context is not None
            assert "prescription_draft_guard" in context
