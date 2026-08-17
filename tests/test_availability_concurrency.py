from __future__ import annotations

from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.core.idempotency import create_fingerprint
from apps.identity.models import Clinic, User, UserClinicRole
from apps.scheduling.locks import (
    acquire_advisory_locks,
    clinic_lock_key,
    user_lock_keys,
)
from apps.scheduling.models import AvailabilityBlock
from apps.scheduling.services import (
    AvailabilityAccessDeniedError,
    AvailabilityIdempotencyConflictError,
    AvailabilityPractitionerError,
)
from apps.tenancy.db import tenant_context
from django.contrib.auth.hashers import make_password
from django.db import connection, transaction

from availability_concurrency_support import (
    acquire_role_revocation_gates,
    assert_no_availability_residue,
    grant_cross_clinic_creator_roles,
    run_retire_create_race,
    start_creator,
    wait_for_advisory_waiter,
)
from availability_service_support import create_synthetic_block
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_timezone_winner_controls_post_gate_utc_and_fingerprint(
    rbac_graph: RbacGraph,
) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        acquire_advisory_locks((clinic_lock_key(rbac_graph.clinic_a),))
        thread, backend_pids, outcomes = start_creator(
            rbac_graph,
            actor_id=rbac_graph.shared_user,
        )
        wait_for_advisory_waiter(backend_pids.get(timeout=5))
        Clinic.objects.filter(pk=rbac_graph.clinic_a).update(timezone="America/Manaus")
    thread.join(timeout=10)

    assert not thread.is_alive()
    result = outcomes.get_nowait()
    assert isinstance(result, AvailabilityBlock)
    assert result.start_at.isoformat() == "2035-04-01T13:00:00+00:00"
    assert bytes(result.create_fingerprint) == create_fingerprint(
        "availability",
        {
            "clinic_id": str(rbac_graph.clinic_a),
            "end_utc": "2035-04-01T14:00:00Z",
            "practitioner_id": str(rbac_graph.physician),
            "start_utc": "2035-04-01T13:00:00Z",
        },
    )


@pytest.mark.parametrize(
    "role",
    [
        UserClinicRole.Role.OWNER,
        UserClinicRole.Role.CLINIC_ADMIN,
        UserClinicRole.Role.RECEPTIONIST,
    ],
)
def test_manager_revocation_winner_denies_waiting_create_without_residue(
    rbac_graph: RbacGraph,
    role: UserClinicRole.Role,
) -> None:
    actor = User.objects.create(
        username=f"todo9-synthetic-{role}-{uuid4().hex}",
        password=make_password(None),
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        assignment = UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user=actor,
            role=role,
        )

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        acquire_role_revocation_gates(rbac_graph.clinic_a, actor.pk)
        thread, backend_pids, outcomes = start_creator(
            rbac_graph,
            actor_id=actor.pk,
        )
        wait_for_advisory_waiter(backend_pids.get(timeout=5))
        assignment.delete()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert isinstance(outcomes.get_nowait(), AvailabilityAccessDeniedError)
    assert_no_availability_residue(rbac_graph)


def test_target_physician_revocation_winner_denies_create_without_residue(
    rbac_graph: RbacGraph,
) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        acquire_role_revocation_gates(rbac_graph.clinic_a, rbac_graph.physician)
        thread, backend_pids, outcomes = start_creator(
            rbac_graph,
            actor_id=rbac_graph.shared_user,
        )
        wait_for_advisory_waiter(backend_pids.get(timeout=5))
        UserClinicRole.objects.filter(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user_id=rbac_graph.physician,
            role=UserClinicRole.Role.PHYSICIAN,
        ).delete()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert isinstance(outcomes.get_nowait(), AvailabilityPractitionerError)
    assert_no_availability_residue(rbac_graph)


def test_create_acquires_clinic_before_practitioner_gate(
    rbac_graph: RbacGraph,
) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        acquire_advisory_locks(user_lock_keys((rbac_graph.physician,)))
        thread, backend_pids, outcomes = start_creator(
            rbac_graph,
            actor_id=rbac_graph.shared_user,
        )
        backend_pid = backend_pids.get(timeout=5)
        wait_for_advisory_waiter(backend_pid)
        cursor.execute(
            "SELECT count(*) FILTER (WHERE granted), "
            "count(*) FILTER (WHERE NOT granted) FROM pg_locks "
            "WHERE pid = %s AND locktype = 'advisory'",
            [backend_pid],
        )
        lock_state = cursor.fetchone()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert lock_state == (1, 1)
    assert isinstance(outcomes.get_nowait(), AvailabilityBlock)


def test_concurrent_equal_key_returns_one_block_and_one_audit(
    rbac_graph: RbacGraph,
) -> None:
    start_barrier = Barrier(2)
    key = uuid4()
    workers = [
        start_creator(
            rbac_graph,
            actor_id=rbac_graph.shared_user,
            idempotency_key=key,
            start_barrier=start_barrier,
        )
        for _ in range(2)
    ]
    for thread, _, _ in workers:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread, _, _ in workers)
    results = [outcomes.get_nowait() for _, _, outcomes in workers]
    blocks = tuple(
        result for result in results if isinstance(result, AvailabilityBlock)
    )
    assert len(blocks) == 2
    assert blocks[0].pk == blocks[1].pk
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
        connection.cursor() as cursor,
    ):
        assert AvailabilityBlock.objects.count() == 1
        cursor.execute("SELECT count(*) FROM clinic_app.audit_event_tenant")
        assert cursor.fetchone() == (1,)


def test_cross_clinic_key_loser_compares_winner_fingerprint(
    rbac_graph: RbacGraph,
) -> None:
    grant_cross_clinic_creator_roles(rbac_graph)
    start_barrier = Barrier(2)
    key = uuid4()
    workers = [
        start_creator(
            rbac_graph,
            actor_id=rbac_graph.shared_user,
            idempotency_key=key,
            clinic_id=clinic_id,
            start_barrier=start_barrier,
        )
        for clinic_id in (rbac_graph.clinic_a, rbac_graph.clinic_b)
    ]
    for thread, _, _ in workers:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread, _, _ in workers)
    results = [outcomes.get_nowait() for _, _, outcomes in workers]
    assert sum(isinstance(result, AvailabilityBlock) for result in results) == 1
    assert (
        sum(
            isinstance(result, AvailabilityIdempotencyConflictError)
            for result in results
        )
        == 1
    )


def test_retire_create_race_serializes_without_orphan_state(
    rbac_graph: RbacGraph,
) -> None:
    with (
        runtime_role(),
        tenant_context(
            rbac_graph.shared_user,
            rbac_graph.organization_a,
        ),
    ):
        original = create_synthetic_block(
            rbac_graph.clinic_a,
            rbac_graph.physician,
            ("2035-04-02", "09:00", "10:00"),
        )
    results = run_retire_create_race(rbac_graph, original.pk)
    with (
        runtime_role(),
        tenant_context(
            rbac_graph.shared_user,
            rbac_graph.organization_a,
        ),
    ):
        blocks = tuple(AvailabilityBlock.objects.order_by("pk"))
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM clinic_app.audit_event_tenant")
            audit_count = cursor.fetchone()

    assert sum(isinstance(result, AvailabilityBlock) for result in results) >= 1
    assert all(
        block.pk != original.pk or block.retired_at is not None for block in blocks
    )
    assert sum(block.retired_at is None for block in blocks) <= 1
    assert audit_count == (len(blocks) + 1,)
