"""Exact arithmetic and explicit catalog/reconciliation binding under FORCE RLS."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ehr.medication_services import MedicationInput, record_medication_statement
from apps.ehr.services import ClinicalAccessDeniedError
from apps.intake.models import PatientClinicEnrollment
from apps.prescription.models import MedicationCatalogEntry, PrescriptionItem
from apps.prescription.services import create_draft, draft_items, save_draft
from apps.tenancy.db import tenant_context
from django.core.exceptions import ValidationError

from ehr.test_autosave import registration
from patient_service_support import runtime_role
from prescription.test_catalog import catalog_file, load_catalog
from renewal.test_prescription_drafts import ITEM, seed

if TYPE_CHECKING:
    from pathlib import Path

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
STRUCTURED = {
    **ITEM,
    "dose": "0.5",
    "dose_unit": "tablet",
    "route": "oral",
    "frequency": "3",
    "duration": "7",
    "quantity": "",
}


def test_structured_quantity_uses_exact_arithmetic_and_free_text_is_retained(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    # Given: an explicitly selected catalog record and entered directions.
    load_catalog(catalog_file(tmp_path))
    entry = MedicationCatalogEntry.objects.get()
    scope = seed(rbac_graph)
    item = {**STRUCTURED, "catalog_entry_id": str(entry.pk)}
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = create_draft(**scope)
        # When: the physician saves, rather than accepting a generated dose.
        saved = save_draft(
            **scope,
            draft_id=draft.pk,
            category=None,
            expected_version=1,
            items=[item],
        )
        # Then: total quantity is 0.5 * 3 * 7 = 10.5 tablets, not rounded.
        stored = PrescriptionItem.objects.get(draft=draft, version=2)
        assert stored.quantity == "10.5"
        assert stored.directions["quantity_value"] == "10.5"
        assert stored.directions["quantity_unit"] == "tablet"
        assert stored.catalog_entry_id == entry.pk
        assert stored.instructions == ITEM["instructions"]
        assert draft_items(saved)[0]["dose"] == "0.5"
        assert saved.category is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dose", "NaN"),
        ("dose", "-1"),
        ("dose", "1e1000000"),
        ("dose_unit", "inferred"),
        ("frequency", "0"),
        ("duration", "forever"),
    ],
)
def test_invalid_structured_directions_preserve_the_saved_version(
    rbac_graph: RbacGraph, field: str, value: str
) -> None:
    # Given: an unset empty draft.
    scope = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = create_draft(**scope)
        # When: a numeric or unit boundary is invalid.
        with pytest.raises(ValidationError):
            save_draft(
                **scope,
                draft_id=draft.pk,
                category=None,
                expected_version=1,
                items=[{**STRUCTURED, field: value}],
            )
        # Then: the invalid request cannot append a snapshot or bump the version.
        draft.refresh_from_db()
        assert draft.version == 1
        assert not PrescriptionItem.objects.exists()


@pytest.mark.parametrize("field", ["catalog_entry_id", "medication_statement_id"])
def test_unknown_record_links_share_the_same_denial(
    rbac_graph: RbacGraph, field: str
) -> None:
    # Given: untrusted selectors in the POST body, never source authority.
    scope = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = create_draft(**scope)
        # When / Then: the service refuses a link it cannot read in this scope.
        with pytest.raises(ClinicalAccessDeniedError):
            save_draft(
                **scope,
                draft_id=draft.pk,
                category=None,
                expected_version=1,
                items=[{**ITEM, field: str(uuid4())}],
            )
        assert not PrescriptionItem.objects.exists()


def test_reconciliation_link_does_not_confirm_or_change_the_statement(
    rbac_graph: RbacGraph,
) -> None:
    # Given: explicitly reported, unreconciled use for the encounter's patient.
    scope = seed(rbac_graph)
    registration(rbac_graph, rbac_graph.physician, rbac_graph.clinic_a)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        enrollment = PatientClinicEnrollment.objects.get(
            patient_id=scope["patient_id"], clinic_id=scope["clinic_id"]
        )
        statement = record_medication_statement(
            clinic_id=scope["clinic_id"],
            enrollment_id=enrollment.pk,
            medication=MedicationInput(
                text="Synthetic reported medicine",
                dose_value="1",
                dose_unit="mg",
                route="oral",
                times_per_day="1",
                provenance="reported_by_patient",
            ),
        )
        draft = create_draft(**scope)
        # When: the physician explicitly associates the prescription item.
        save_draft(
            **scope,
            draft_id=draft.pk,
            category=None,
            expected_version=1,
            items=[{**ITEM, "medication_statement_id": str(statement.pk)}],
        )
        # Then: a link is stored, not a medication reconciliation or new order.
        stored = PrescriptionItem.objects.get(draft=draft)
        assert stored.medication_statement_id == statement.pk
        statement.refresh_from_db()
        assert statement.provenance == "reported_by_patient"
        assert statement.reconciled_at is None
