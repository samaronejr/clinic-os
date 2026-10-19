"""Native form, alias-aware search and immutable raw-DML regression proofs."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.intake import services
from apps.intake.demographics import DEMOGRAPHICS_FIELD_VALUES
from apps.intake.models import MergeDecision
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction
from django.urls import reverse

from identity.lifecycle_test_support import session_key, verified_client
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from patients.duplicate_history_support import retained_histories
from patients.duplicate_support import _merged_pair, _pair

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph


def test_native_review_form_applies_displayed_versions_and_requires_csrf(
    rbac_graph: RbacGraph,
) -> None:
    first, second, candidate = _pair(rbac_graph, services)
    client = verified_client(rbac_graph)
    url = reverse("intake:patient-duplicates", args=(rbac_graph.clinic_a,))
    with runtime_role():
        opened = client.post(url, {"action": "review", "candidate_id": candidate.pk})
    assert opened.status_code == 200
    assert opened.context["first"].patient_id in {first.patient.pk, second.patient.pk}
    body = {
        "action": "merge",
        "candidate_id": str(candidate.pk),
        "survivor_patient_id": str(first.patient.pk),
        "expected_a": "0",
        "expected_b": "0",
        "reason": "Synthetic native reviewed choice",
        **{
            f"choice_{field}": str(first.patient.pk)
            for field in DEMOGRAPHICS_FIELD_VALUES
        },
    }
    client.handler.enforce_csrf_checks = True
    with runtime_role():
        rejected = client.post(url, body)
    assert rejected.status_code == 403
    with owner_context(rbac_graph.organization_a):
        assert not MergeDecision.objects.filter(candidate=candidate).exists()
    body["csrfmiddlewaretoken"] = client.cookies["csrftoken"].value
    with runtime_role():
        accepted = client.post(url, body)
    assert accepted.status_code == 200
    assert accepted.context["review"].decision.survivor_patient_id == first.patient.pk
    assert accepted.context["review"].decision.result_version == 1


def test_search_matches_retained_identity_but_selects_survivor(
    rbac_graph: RbacGraph,
) -> None:
    first, second, _decision, _key = _merged_pair(rbac_graph, services)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        page = services.search_patients(
            clinic_id=rbac_graph.clinic_a, query=second.patient.full_name, page=1
        )
        assert page.items
        assert all(item.enrollment_id == first.enrollment.pk for item in page.items)
        assert all(item.unified_at is not None for item in page.items)
        exact = services.search_patient_identifiers(
            clinic_id=rbac_graph.clinic_a, kind="cpf", value="52998224725"
        )
        assert all(item.enrollment_id == first.enrollment.pk for item in exact.items)


def test_wrong_clinic_and_unknown_candidate_have_identical_denials(
    rbac_graph: RbacGraph,
) -> None:
    first, _second, candidate = _pair(rbac_graph, services)
    key = session_key(verified_client(rbac_graph))
    for candidate_id in (candidate.pk, uuid4()):
        with (
            runtime_role(),
            tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
            services.duplicate_review_session(
                clinic_id=rbac_graph.clinic_b, session_key=key
            ),
            pytest.raises(services.PatientAccessDeniedError),
        ):
            services.adjudicate_duplicate(
                clinic_id=rbac_graph.clinic_b,
                candidate_id=candidate_id,
                decision="merge",
                survivor_patient_id=first.patient.pk,
                field_choices={},
                reason="Synthetic wrong-clinic probe",
            )


def test_native_encounter_repoint_denied_by_acl_and_owner_by_trigger(
    rbac_graph: RbacGraph,
) -> None:
    world = retained_histories(rbac_graph, services)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        with (
            pytest.raises(DatabaseError) as native,
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE clinic_app.ehr_encounter SET patient_id=%s WHERE id=%s",
                [world.first.patient.pk, world.encounter_ids[1]],
            )
        assert getattr(native.value.__cause__, "sqlstate", None) == "42501"
    with owner_context(rbac_graph.organization_a):
        with (
            pytest.raises(DatabaseError) as guarded,
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE clinic_app.ehr_encounter SET patient_id=%s WHERE id=%s",
                [world.first.patient.pk, world.encounter_ids[1]],
            )
        assert getattr(guarded.value.__cause__, "sqlstate", None) == "23514"
