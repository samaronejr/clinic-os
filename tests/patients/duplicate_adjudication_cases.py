"""Choice-version binding, deterministic concurrent adjudication and audit."""

from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING

from apps.audit.models import AuditEvent
from apps.intake.models import MergeDecision, Patient, PatientDemographics
from apps.tenancy.db import tenant_context
from django.db import connections

from identity.lifecycle_test_support import session_key, verified_client
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from patients.duplicate_support import _pair

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph


def test_choices_change_only_a_new_survivor_version_and_keep_source_history(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.intake.services")
    # Given: one reviewed source version and a distinct survivor registry name.
    first, second, candidate = _pair(rbac_graph, services)
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        source = services.update_demographics(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=second.enrollment.pk,
            expected_version=0,
            changes={"social_name": "Synthetic reviewed source"},
            reason="Synthetic source correction",
        )
        source_values = (source.legal_name, source.social_name, source.version)
        survivor_before = first.patient.full_name
    client = verified_client(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        services.duplicate_review_session(
            clinic_id=rbac_graph.clinic_a, session_key=session_key(client)
        ),
    ):
        # When: the human selects the exact displayed source version.
        decision = services.adjudicate_duplicate(
            clinic_id=rbac_graph.clinic_a,
            candidate_id=candidate.pk,
            decision="merge",
            survivor_patient_id=first.patient.pk,
            field_choices={
                "legal_name": services.DemographicFieldChoice(second.patient.pk, 1),
                "social_name": services.DemographicFieldChoice(second.patient.pk, 1),
            },
            reason="Synthetic field choices adjudicated",
        )
        # Then: the permitted registry UPDATE actually changed stored identity.
        survivor = Patient.objects.get(pk=first.patient.pk)
        assert survivor.full_name != survivor_before
        assert survivor.full_name == source_values[0]
        current = PatientDemographics.objects.get(
            patient_id=first.patient.pk, version=decision.result_version
        )
        assert current.social_name == source_values[1]
        source.refresh_from_db()
        assert (source.legal_name, source.social_name, source.version) == source_values


def test_concurrent_sessions_adjudicate_one_pair_once(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.intake.services")
    # Given: two genuinely verified sessions and the same open candidate.
    first, _second, candidate = _pair(rbac_graph, services)
    keys = tuple(session_key(verified_client(rbac_graph)) for _ in range(2))
    barrier = Barrier(2, timeout=20)

    def merge(key: str) -> str:
        try:
            with (
                runtime_role(),
                tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
                services.duplicate_review_session(
                    clinic_id=rbac_graph.clinic_a, session_key=key
                ),
            ):
                # When: both live sessions reach the same command boundary.
                barrier.wait()
                try:
                    services.adjudicate_duplicate(
                        clinic_id=rbac_graph.clinic_a,
                        candidate_id=candidate.pk,
                        decision="merge",
                        survivor_patient_id=first.patient.pk,
                        field_choices={},
                        reason="Synthetic concurrent adjudication",
                    )
                except services.DuplicateConflictError as error:
                    return str(error)
                return "merged"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(merge, keys))
    # Then: one decision wins and the other session observes a state conflict.
    assert sorted(outcomes) == ["duplicate_stale", "merged"]
    with owner_context(rbac_graph.organization_a):
        assert MergeDecision.objects.filter(candidate_id=candidate.pk).count() == 1
        events = tuple(AuditEvent.objects.filter(event_type="intake.duplicate.merged"))
        assert len(events) == 1
        assert set(events[0].payload) <= {
            "clinic_id",
            "http_method",
            "http_status",
            "object_verb",
            "reason_code",
            "request_id",
        }
