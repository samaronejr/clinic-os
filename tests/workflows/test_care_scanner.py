"""Todo 52 B5: the care scanner never stalls, swallows a refusal or starves a tenant.

Every case drives the real beat (``scan_care``) and worker (``run_care``) on
the injected database clock; nothing calls a transition directly.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.identity.models import CareTeamMembership, User, UserClinicRole
from apps.intake.models import Patient, PatientClinicEnrollment
from apps.tenancy.db import tenant_context
from apps.workflows.care import services as care
from apps.workflows.care.models import CareAction, CareRefusal, Recall
from apps.workflows.care.vocab import SCAN_PER_TENANT, SCAN_RETRY_MINUTES
from apps.workflows.validation import TaskOwner
from django.utils import timezone

from identity.permission_support import owner_context
from patient_service_support import runtime_role
from workflows.clock_support import database_clock
from workflows.permission_support import permission_actor
from workflows.test_care_privacy import care_clinician
from workflows.test_careplans import (
    acting,
    amended,
    plan_of,
    pro_terms,
    return_in,
    rows,
    run_jobs,
    scan,
    world,
)

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def recall_for(
    graph: RbacGraph, staff: UUID, enrollment: UUID, organization: UUID | None = None
) -> Recall:
    clinic = graph.clinic_a if organization is None else graph.clinic_c
    with runtime_role(), tenant_context(staff, organization or graph.organization_a):
        return care.schedule_recall(
            clinic_id=clinic,
            enrollment_id=enrollment,
            reason="routine_return",
            interval_days=1,
            owner_role="receptionist",
            idempotency_key=uuid4(),
        )


def refusals(graph: RbacGraph) -> list[CareRefusal]:
    return rows(graph, CareRefusal)


def test_refused_creator_is_recorded_parked_and_retried_after_the_delay(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    creator, enrollment = permission_actor(graph, "clinic_manager")
    base = timezone.now()
    clock = {"now": base}
    with database_clock(graph.organization_a, lambda: clock["now"]) as advance:
        recall = recall_for(graph, creator, enrollment)
        with owner_context(graph.organization_a):
            UserClinicRole.objects.filter(user_id=creator).update(
                revoked_at=timezone.now()
            )
        clock["now"] = base + timedelta(days=2)
        advance()
        jobs = scan(monkeypatch)
        assert jobs == [{"kind": "recall", "record_id": str(recall.pk)}]
        assert run_jobs(jobs) == [False]
        (refused,) = refusals(graph)
        assert (refused.kind, refused.record_id, refused.actor_id) == (
            "recall",
            recall.pk,
            creator,
        )
        assert refused.attempts == 1
        assert refused.retry_at - refused.refused_at == timedelta(
            minutes=SCAN_RETRY_MINUTES
        )
        # Recorded, not swallowed, and not listed again inside the delay.
        assert scan(monkeypatch) == []
        clock["now"] = base + timedelta(days=2, minutes=SCAN_RETRY_MINUTES + 1)
        advance()
        assert run_jobs(scan(monkeypatch)) == [False]
        assert refusals(graph)[0].attempts == 2
        # Authority restored: the next retry materializes the recall once.
        with owner_context(graph.organization_a):
            UserClinicRole.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                user_id=creator,
                role="clinic_manager",
                workflow_bundle_version=2,
            )
        clock["now"] = base + timedelta(days=2, minutes=2 * SCAN_RETRY_MINUTES + 2)
        advance()
        assert run_jobs(scan(monkeypatch)) == [True]
        assert scan(monkeypatch) == []
    assert rows(graph, Recall, pk=recall.pk)[0].state == "materialized"


def test_refusal_evidence_is_read_by_managers_only(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    creator, enrollment = permission_actor(graph, "clinic_manager")
    base = timezone.now()
    clock = {"now": base}
    with database_clock(graph.organization_a, lambda: clock["now"]) as advance:
        recall_for(graph, creator, enrollment)
        User.objects.filter(pk=creator).update(is_active=False)
        clock["now"] = base + timedelta(days=2)
        advance()
        assert run_jobs(scan(monkeypatch)) == [False]
    boss, _own = permission_actor(graph, "clinic_manager")
    clerk, _own = permission_actor(graph, "receptionist")
    with acting(graph, boss):
        assert CareRefusal.objects.count() == 1
    with acting(graph, clerk):
        assert CareRefusal.objects.count() == 0


def test_record_hidden_from_its_creator_is_recorded_not_dropped(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    chart, _version = world(graph)
    clinician = care_clinician(graph, "physician", chart.enrollment)
    start = timezone.now()
    clock = {"now": start}
    with database_clock(graph.organization_a, lambda: clock["now"]) as advance:
        with acting(graph, clinician):
            care.assign_pro(
                clinic_id=graph.clinic_a,
                enrollment_id=chart.enrollment,
                spec=pro_terms(first_due_at=start + timedelta(days=1), occurrences=1),
                idempotency_key=uuid4(),
            )
        # The clinician's care-team membership is revoked: the slot is clinical
        # data they may no longer see, so their job cannot close it.
        with owner_context(graph.organization_a):
            CareTeamMembership.objects.filter(user_id=clinician).update(
                revoked_at=timezone.now()
            )
        clock["now"] = start + timedelta(days=10)
        advance()
        jobs = scan(monkeypatch)
        assert [job["kind"] for job in jobs] == ["pro_missed"]
        assert run_jobs(jobs) == [False]
        assert scan(monkeypatch) == []
    (refused,) = refusals(graph)
    assert (refused.kind, refused.actor_id) == ("pro_missed", clinician)


def test_flagged_action_past_its_window_is_neither_listed_nor_marked(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    _chart, version = world(graph)
    plan = plan_of(graph, version, return_in(1))
    base = timezone.now()
    clock = {"now": base}
    with database_clock(graph.organization_a, lambda: clock["now"]) as advance:
        amended(graph, version)
        assert run_jobs(scan(monkeypatch)) == [True]
        clock["now"] = base + timedelta(weeks=3)
        advance()
        assert scan(monkeypatch) == []
    action = rows(graph, CareAction, plan_id=plan.pk)[0]
    assert (action.review_state, action.state) == ("required", "open")
    assert refusals(graph) == []


def _second_tenant(graph: RbacGraph) -> tuple[UUID, UUID]:
    """A v2 clinic manager and one enrollment in the other organization's clinic."""
    staff = User.objects.create(username=f"sintetico-care-scan-{uuid4().hex}").pk
    with owner_context(graph.organization_b):
        UserClinicRole.objects.create(
            organization_id=graph.organization_b,
            clinic_id=graph.clinic_c,
            user_id=staff,
            role="clinic_manager",
            workflow_bundle_version=2,
        )
        patient = Patient.objects.create(
            organization_id=graph.organization_b,
            full_name="Sintetico Outra Clinica",
            birth_date=date(1990, 1, 1),
        )
        enrollment = PatientClinicEnrollment.objects.create(
            organization_id=graph.organization_b,
            clinic_id=graph.clinic_c,
            patient=patient,
            idempotency_key=uuid4(),
            create_fingerprint=b"s" * 32,
        )
    return staff, enrollment.pk


def test_one_tenants_backlog_never_starves_another(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    busy, enrollment = permission_actor(graph, "clinic_manager")
    quiet, other = _second_tenant(graph)
    base = timezone.now()
    clock = {"now": base}
    with database_clock(graph.organization_a, lambda: clock["now"]) as advance:
        backlog = [recall_for(graph, busy, enrollment).pk]
        for minute in range(1, SCAN_PER_TENANT + 1):
            clock["now"] = base + timedelta(minutes=minute)
            advance()
            backlog.append(recall_for(graph, busy, enrollment).pk)
        clock["now"] = base + timedelta(minutes=SCAN_PER_TENANT + 5)
        advance()
        late = recall_for(graph, quiet, other, organization=graph.organization_b)
        clock["now"] = base + timedelta(days=3)
        advance()
        listed = {UUID(job["record_id"]) for job in scan(monkeypatch)}
    # The older backlog is capped per organization; the newer tenant is served.
    assert late.pk in listed
    assert listed - {late.pk} == set(backlog[:SCAN_PER_TENANT])
    assert backlog[-1] not in listed


def test_assigned_work_on_a_stale_plan_keeps_its_owner_and_waits(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    _chart, version = world(graph)
    plan = plan_of(graph, version, return_in(6))
    action = rows(graph, CareAction, plan_id=plan.pk)[0]
    staff, _own = permission_actor(graph, "clinic_manager")
    with acting(graph, staff):
        care.assign_care_action(
            clinic_id=graph.clinic_a,
            action_id=action.pk,
            owner=TaskOwner(user_id=staff),
            expected_revision=action.revision,
        )
    amended(graph, version)
    assert run_jobs(scan(monkeypatch)) == [True]
    flagged = rows(graph, CareAction, pk=action.pk)[0]
    assert (flagged.state, flagged.owner_user_id, flagged.review_state) == (
        "assigned",
        staff,
        "required",
    )
