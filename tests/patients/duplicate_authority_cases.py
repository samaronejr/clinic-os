"""Every stored role is checked against real session and native SQL authority."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import SessionRecord, UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from apps.intake import merge_execution, services
from apps.intake.alias_access import MERGE_ROLES
from apps.intake.models import MergeDecision
from apps.tenancy.db import tenant_context
from django.db import connection, transaction

from auth.stepup_test_support import create_role_actor
from identity.lifecycle_test_support import session_key, verified_client
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from patients.duplicate_support import _pair

if TYPE_CHECKING:
    from typing import Never
    from uuid import UUID

    from rbac_fixtures import RbacGraph

ACTOR_PROTOCOL_ORACLES = {
    "clinic_app.intake_merge_mutation_allowed(uuid,uuid)": (
        "patients.duplicate_authority_cases."
        "test_merge_mutation_context_refuses_inactive_actor",
    ),
}

SQL_PROTOCOL_ORACLES = {
    name: ["test_merge_authority_checks_every_stored_role_and_exact_live_session"]
    for name in (
        "clinic_app.intake_duplicate_detection_guard",
        "clinic_app.intake_duplicate_transition_guard",
        "clinic_app.intake_merge_authorized",
        "clinic_app.intake_merge_decision_guard",
        "clinic_app.intake_merge_mutation_allowed",
        "clinic_app.intake_merge_receipt_guard",
        "clinic_app.intake_patient_alias_guard",
        "clinic_app.patient_alias_set",
    )
} | {
    "clinic_app.intake_merge_transfer_guard": [
        "test_merge_recreates_forward_rows_with_links_and_fresh_access",
        "test_forward_waitlist_keeps_integer_source_and_replacement_links",
    ],
    "clinic_app.intake_survivor_write_guard": [
        "test_new_charge_for_an_aliased_identity_is_rejected",
    ],
}


@pytest.mark.parametrize("role", UserClinicRole.Role.values)
def test_merge_authority_checks_every_stored_role_and_exact_live_session(
    rbac_graph: RbacGraph,
    role: str,
) -> None:
    first, second, candidate = _pair(rbac_graph, services)
    actor = create_role_actor(rbac_graph, UserClinicRole.Role(role))
    key = session_key(verified_client(replace(rbac_graph, clinic_admin=actor.pk)))
    digest = sha256(key.encode()).digest()
    with owner_context(rbac_graph.organization_a):
        proof = SessionRecord.objects.get(user=actor, session_key_hash=digest)
    permitted = role in MERGE_ROLES and "demographics.read" in BUNDLES_V1[role]
    with runtime_role(), tenant_context(actor.pk, rbac_graph.organization_a):
        if "demographics.read" in BUNDLES_V1[role]:
            assert any(
                row.pk == candidate.pk
                for row in services.duplicate_candidates(clinic_id=rbac_graph.clinic_a)
            )
        else:
            with pytest.raises(services.PatientAccessDeniedError):
                services.duplicate_candidates(clinic_id=rbac_graph.clinic_a)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_merge_session',%s,true)", [digest.hex()]
            )
            cursor.execute(
                "SELECT clinic_app.intake_merge_authorized(%s,%s), "
                "clinic_app.intake_merge_authorized(%s,%s), "
                "clinic_app.intake_merge_mutation_allowed(%s,%s)",
                [
                    rbac_graph.clinic_a,
                    proof.pk,
                    rbac_graph.clinic_a,
                    uuid4(),
                    rbac_graph.clinic_a,
                    first.patient.pk,
                ],
            )
            assert cursor.fetchone() == (permitted, False, False)
        if not permitted:
            with pytest.raises(services.PatientAccessDeniedError):
                services.adjudicate_duplicate(
                    clinic_id=rbac_graph.clinic_a,
                    candidate_id=candidate.pk,
                    decision="merge",
                    survivor_patient_id=first.patient.pk,
                    field_choices={},
                    reason="Synthetic role refusal",
                )
            return
        with services.duplicate_review_session(
            clinic_id=rbac_graph.clinic_a, session_key=key
        ):
            decision = services.adjudicate_duplicate(
                clinic_id=rbac_graph.clinic_a,
                candidate_id=candidate.pk,
                decision="merge",
                survivor_patient_id=first.patient.pk,
                field_choices={
                    "legal_name": services.DemographicFieldChoice(second.patient.pk, 0)
                },
                reason="Synthetic exact role and session adjudication",
            )
            projection = services.patient_alias_set(
                clinic_id=rbac_graph.clinic_a, patient_id=first.patient.pk
            )
            assert set(projection.patient_ids) == {first.patient.pk, second.patient.pk}
            assert decision.adjudicator_id == actor.pk
            services.unmerge(
                clinic_id=rbac_graph.clinic_a,
                merge_decision_id=decision.pk,
                reason="Synthetic role reversal",
            )
            assert services.patient_alias_set(
                clinic_id=rbac_graph.clinic_a, patient_id=second.patient.pk
            ).patient_ids == (second.patient.pk,)


class MutationProbeCompleteError(Exception):
    """Roll back a genuine prepared merge after observing its native capability."""


def _mutation_allowed(clinic_id: UUID, patient_id: UUID) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_user, clinic_app.intake_merge_mutation_allowed(%s,%s)",
            [clinic_id, patient_id],
        )
        row = cursor.fetchone()
    assert row is not None
    role, allowed = row
    assert role == "clinic_app"
    assert type(allowed) is bool
    return allowed


def test_merge_mutation_context_refuses_inactive_actor(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _second, candidate = _pair(rbac_graph, services)
    key = session_key(verified_client(rbac_graph))
    observed: list[bool] = []

    def inspect_transfer(*, clinic_id: UUID, merge_decision_id: UUID) -> Never:
        decision = MergeDecision.objects.get(pk=merge_decision_id)
        observed.append(_mutation_allowed(clinic_id, decision.merged_patient_id))
        # Keep the real decision, actor GUC and verified session. Only the
        # account's native lifecycle state changes, inside a rolled-back savepoint.
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL ROLE clinic_owner")
                cursor.execute(
                    "UPDATE clinic_app.identity_user SET is_active=false WHERE id=%s",
                    [rbac_graph.clinic_admin],
                )
                assert cursor.rowcount == 1
                cursor.execute("SET LOCAL ROLE clinic_app")
            observed.append(_mutation_allowed(clinic_id, decision.merged_patient_id))
            transaction.set_rollback(True)
        observed.append(_mutation_allowed(clinic_id, decision.merged_patient_id))
        raise MutationProbeCompleteError

    monkeypatch.setattr(merge_execution, "transfer_intake", inspect_transfer)
    with (
        pytest.raises(MutationProbeCompleteError),
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        services.duplicate_review_session(
            clinic_id=rbac_graph.clinic_a, session_key=key
        ),
    ):
        services.adjudicate_duplicate(
            clinic_id=rbac_graph.clinic_a,
            candidate_id=candidate.pk,
            decision="merge",
            survivor_patient_id=first.patient.pk,
            field_choices={},
            reason="Synthetic native activity capability probe",
        )
    assert observed == [True, False, True]
