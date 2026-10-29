"""Exact local observations and provenance, exercised on real tenant roles."""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ehr.longitudinal import (
    MedicationInput,
    MedicationReconciliation,
    ObservationInput,
    TimelineFilters,
    confirm_observation,
    reconcile_medications,
    record_medication_statement,
    record_observation,
    record_vitals,
    timeline,
)
from apps.ehr.longitudinal_values import bmi, decimal_string, observation_value
from apps.ehr.models import MedicationStatement, Observation
from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.utils.translation import gettext

from ehr.longitudinal_support import chart_world, medication_count
from ehr.test_autosave import as_actor
from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

MEDICATION = MedicationInput(
    text="SINTETICO-SENTINELA-28 medication",
    dose_value="5,00",
    dose_unit="mg",
    route="oral",
    times_per_day="2",
    provenance="reported_by_patient",
)
TEMPERATURE = ObservationInput(
    kind="temperature",
    value="37,5",
    unit="Cel",
    provenance="reported_by_patient",
    reference_low="36,0",
    reference_high="38,0",
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("37,5", "37.5"), ("37.50", "37.50"), ("0", "0"), ("-2,5", "-2.5")],
)
def test_decimal_input_is_exact(value: str, expected: str) -> None:
    assert decimal_string(value) == expected


@pytest.mark.parametrize("value", ["1.000,5", "1,000.5", "NaN", "Infinity", "1e3", ""])
def test_ambiguous_or_nonfinite_decimal_is_refused(value: str) -> None:
    with pytest.raises(ValidationError):
        decimal_string(value)


def test_bmi_is_decimal_not_float_or_ai() -> None:
    assert bmi(weight="70,0", height="175") == "22.86"
    assert Decimal(bmi(weight="70", height="175")) == Decimal("22.86")
    with localcontext() as context:
        context.prec = 2
        assert bmi(weight="70", height="175") == "22.86"


def test_observation_unit_is_bound_to_the_versioned_kind() -> None:
    with pytest.raises(ValidationError):
        observation_value(kind="temperature", value="37,5", unit="kg")
    assert observation_value(kind="temperature", value="37,5", unit="Cel") == "37.5"


@pytest.mark.django_db(transaction=True)
def test_medication_origin_survives_explicit_reconciliation(
    rbac_graph: RbacGraph,
) -> None:
    world = chart_world(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.physician):
        row = record_medication_statement(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=world.enrollment,
            medication=MEDICATION,
        )
        assert (row.status, row.provenance, row.reconciled_at) == (
            "unknown",
            "reported_by_patient",
            None,
        )
        assert row.details["dose"] == {"value": "5.00", "unit": "mg"}
        (reviewed,) = reconcile_medications(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=world.enrollment,
            statements=(
                MedicationReconciliation(
                    statement_id=row.pk, expected_revision=1, status="active"
                ),
            ),
        )
        assert (reviewed.origin, reviewed.provenance, reviewed.status) == (
            "reported_by_patient",
            "clinician_confirmed",
            "active",
        )
        assert reviewed.reconciled_by_id == rbac_graph.physician
        with pytest.raises(ClinicalConflictError):
            reconcile_medications(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=world.enrollment,
                statements=(
                    MedicationReconciliation(
                        statement_id=row.pk, expected_revision=1, status="stopped"
                    ),
                ),
            )


@pytest.mark.django_db(transaction=True)
def test_observation_value_ranges_and_confirmation_are_exact(
    rbac_graph: RbacGraph,
) -> None:
    world = chart_world(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.physician):
        row = record_observation(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=world.enrollment,
            observation=TEMPERATURE,
        )
        assert row.value["value"] == "37.5"
        assert row.value["reference_low"] == "36.0"
        assert row.provenance == "reported_by_patient"
        confirmed = confirm_observation(
            clinic_id=rbac_graph.clinic_a, observation_id=row.pk, expected_revision=1
        )
        assert confirmed.origin == "reported_by_patient"
        assert confirmed.provenance == "clinician_confirmed"
        with pytest.raises(DatabaseError), transaction.atomic():
            Observation.objects.filter(pk=row.pk).update(
                provenance="observed", revision=3
            )


@pytest.mark.django_db(transaction=True)
def test_vitals_bmi_preserves_reported_origin_and_inputs(rbac_graph: RbacGraph) -> None:
    world = chart_world(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.physician):
        rows = record_vitals(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=world.enrollment,
            measurements=tuple(
                ObservationInput(
                    kind=kind, value=value, unit=unit, provenance="reported_by_patient"
                )
                for kind, value, unit in (
                    ("weight", "70,0", "kg"),
                    ("height", "175", "cm"),
                )
            ),
        )
        calculated = next(row for row in rows if row.kind == "bmi")
        assert calculated.value["value"] == "22.86"
        assert set(calculated.value["derived_from"]) == {
            str(row.pk) for row in rows if row.kind != "bmi"
        }
        assert {row.provenance for row in rows} == {"reported_by_patient"}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("role", ["nurse", "allied_professional"])
def test_nursing_scope_records_but_cannot_confirm_or_reconcile(
    rbac_graph: RbacGraph, role: str
) -> None:
    actor, enrollment = permission_actor(rbac_graph, role)
    with permission_context(rbac_graph, actor):
        row = record_observation(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=enrollment,
            observation=TEMPERATURE,
        )
        with pytest.raises(ClinicalAccessDeniedError):
            confirm_observation(
                clinic_id=rbac_graph.clinic_a,
                observation_id=row.pk,
                expected_revision=1,
            )
        with pytest.raises(ClinicalAccessDeniedError):
            record_medication_statement(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=enrollment,
                medication=MEDICATION,
            )
        assert (
            timeline(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=enrollment,
                filters=TimelineFilters(),
            ).total
            == 0
        )  # no worker has acknowledged the committed source yet
    assert medication_count(rbac_graph) == 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("origin", ["observed", "imported", "ai_drafted"])
def test_creation_does_not_promote_any_origin(
    rbac_graph: RbacGraph, origin: str
) -> None:
    actor, enrollment = permission_actor(rbac_graph, "physician")
    with permission_context(rbac_graph, actor):
        row = record_observation(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=enrollment,
            observation=ObservationInput(
                kind="pain_present", value="unknown", unit="1", provenance=origin
            ),
        )
        assert (row.origin, row.provenance, row.confirmed_at) == (origin, origin, None)


@pytest.mark.django_db(transaction=True)
def test_invalid_unit_and_foreign_selectors_write_nothing(
    rbac_graph: RbacGraph,
) -> None:
    world = chart_world(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.physician):
        with pytest.raises(ValidationError) as failure:
            record_observation(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=world.enrollment,
                observation=ObservationInput(
                    kind="temperature", value="37,5", unit="kg", provenance="observed"
                ),
            )
        assert failure.value.messages == [
            gettext("Choose a valid unit for this observation.")
        ]
        for enrollment in (uuid4(), world.enrollment):
            with pytest.raises(ClinicalAccessDeniedError):
                record_observation(
                    clinic_id=rbac_graph.clinic_b,
                    enrollment_id=enrollment,
                    observation=TEMPERATURE,
                )
    with owner_context(rbac_graph.organization_a):
        assert Observation.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_clinical_values_exist_only_as_envelopes(rbac_graph: RbacGraph) -> None:
    world = chart_world(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.physician):
        row = record_medication_statement(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=world.enrollment,
            medication=MEDICATION,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT details FROM clinic_app.ehr_medicationstatement WHERE id=%s",
                [row.pk],
            )
            ciphertext = bytes(cursor.fetchone()[0])
        assert MEDICATION.text.encode() not in ciphertext
        assert (
            MedicationStatement.objects.get(pk=row.pk).details["text"]
            == MEDICATION.text
        )
