"""Native alias gates protect overlapping decisions outside the service path."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from typing import TYPE_CHECKING

from apps.identity.models import SessionRecord
from apps.intake import services
from apps.intake.models import MergeDecision
from apps.tenancy.db import tenant_context
from django.db import connection, connections, transaction

from identity.lifecycle_test_support import session_key, verified_client
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from patients.duplicate_support import _pair

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph


def test_native_merge_decision_holds_both_alias_write_gates(
    rbac_graph: RbacGraph,
) -> None:
    # Given: a real review session and an ordered pair, without service locks.
    first, second, candidate = _pair(rbac_graph, services)
    key = session_key(verified_client(rbac_graph))
    with owner_context(rbac_graph.organization_a):
        proof = SessionRecord.objects.get(
            session_key_hash=sha256(key.encode()).digest()
        )
        verified_at = proof.verified_at
        assert verified_at is not None
    gate_keys = tuple(
        f"intake-alias-write-v1:{rbac_graph.organization_a}:{patient_id}"
        for patient_id in sorted((first.patient.pk, second.patient.pk))
    )

    def probe_gates() -> tuple[bool, ...]:
        try:
            with (
                runtime_role(),
                tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
                connection.cursor() as cursor,
            ):
                results = []
                for gate in gate_keys:
                    cursor.execute(
                        "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))",
                        [gate],
                    )
                    results.append(cursor.fetchone() == (True,))
                return tuple(results)
        finally:
            connections.close_all()

    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        services.duplicate_review_session(
            clinic_id=rbac_graph.clinic_a, session_key=key
        ),
        transaction.atomic(),
    ):
        # When: native DML creates a decision without the Python lock helper.
        MergeDecision.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            candidate=candidate,
            decision="merge",
            adjudicator_id=rbac_graph.clinic_admin,
            verified_session=proof,
            step_up_verified_at=verified_at,
            merged_patient=second.patient,
            survivor_patient=first.patient,
            merged_version=0,
            survivor_version=0,
            result_version=1,
            reason="Synthetic native gate review",
            field_choices={},
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            gates = executor.submit(probe_gates).result(timeout=20)
        # This intentionally incomplete command is aborted, not committed.
        transaction.set_rollback(True)
    # Then: other native decisions and new writes cannot cross either identity.
    assert gates == (False, False)
