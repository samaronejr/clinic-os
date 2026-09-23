from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import Clinic, User, UserClinicRole
from apps.scheduling.services import (
    AvailabilityAccessDeniedError,
    AvailabilityCreateInputError,
    AvailabilityPractitionerError,
    create_availability,
    retire_availability,
    view_availability,
)
from apps.tenancy.db import tenant_context
from django.contrib.auth.hashers import make_password
from django.db import connection, transaction

from patient_service_support import runtime_role

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "role",
    [
        UserClinicRole.Role.OWNER,
        UserClinicRole.Role.CLINIC_ADMIN,
        UserClinicRole.Role.RECEPTIONIST,
    ],
)
def test_each_manager_role_can_create_view_and_retire(
    rbac_graph: RbacGraph,
    role: UserClinicRole.Role,
) -> None:
    actor = User.objects.create(
        username=f"todo9-synthetic-manager-{role}-{uuid4().hex}",
        password=make_password(None),
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user=actor,
            role=role,
        )
    with runtime_role(), tenant_context(actor.pk, rbac_graph.organization_a):
        block = create_availability(
            clinic_id=rbac_graph.clinic_a,
            practitioner_id=rbac_graph.physician,
            start_local="2035-02-03T09:00",
            end_local="2035-02-03T10:00",
            idempotency_key=uuid4(),
        )
        items = view_availability(clinic_id=rbac_graph.clinic_a)
        retired = retire_availability(
            clinic_id=rbac_graph.clinic_a,
            availability_id=block.pk,
        )

    assert [item.availability_id for item in items] == [block.pk]
    assert retired.retired_at is not None


def test_physician_views_only_own_blocks_and_mutations_hide_targets(
    rbac_graph: RbacGraph,
) -> None:
    other_physician = User.objects.create(
        username=f"todo9-synthetic-physician-{uuid4().hex}",
        password=make_password(None),
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user=other_physician,
            role=UserClinicRole.Role.PHYSICIAN,
        )

    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        own = create_availability(
            clinic_id=rbac_graph.clinic_a,
            practitioner_id=rbac_graph.physician,
            start_local="2035-02-01T09:00",
            end_local="2035-02-01T10:00",
            idempotency_key=uuid4(),
        )
        create_availability(
            clinic_id=rbac_graph.clinic_a,
            practitioner_id=other_physician.pk,
            start_local="2035-02-01T09:00",
            end_local="2035-02-01T10:00",
            idempotency_key=uuid4(),
        )

    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        items = view_availability(clinic_id=rbac_graph.clinic_a)
        with pytest.raises(AvailabilityAccessDeniedError) as existing_error:
            retire_availability(
                clinic_id=rbac_graph.clinic_a,
                availability_id=own.pk,
            )
        with pytest.raises(AvailabilityAccessDeniedError) as missing_error:
            retire_availability(
                clinic_id=rbac_graph.clinic_a,
                availability_id=uuid4(),
            )
        with pytest.raises(AvailabilityAccessDeniedError):
            create_availability(
                clinic_id=rbac_graph.clinic_a,
                practitioner_id=rbac_graph.physician,
                start_local="2035-02-01T10:00",
                end_local="2035-02-01T11:00",
                idempotency_key=uuid4(),
            )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM clinic_app.audit_event_tenant "
                "WHERE event_type = 'scheduling.availability.viewed'"
            )
            view_audit_count = cursor.fetchone()

    assert [item.availability_id for item in items] == [own.pk]
    assert type(existing_error.value) is type(missing_error.value)
    assert existing_error.value.args == missing_error.value.args
    assert view_audit_count == (1,)


def test_unassigned_and_foreign_clinic_scope_denials_are_indistinguishable(
    rbac_graph: RbacGraph,
) -> None:
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        block = create_availability(
            clinic_id=rbac_graph.clinic_a,
            practitioner_id=rbac_graph.physician,
            start_local="2035-02-02T09:00",
            end_local="2035-02-02T10:00",
            idempotency_key=uuid4(),
        )
        denials: list[AvailabilityAccessDeniedError] = []
        for clinic_id in (rbac_graph.clinic_b, rbac_graph.clinic_c):
            with pytest.raises(AvailabilityAccessDeniedError) as view_error:
                view_availability(clinic_id=clinic_id)
            denials.append(view_error.value)
            with pytest.raises(AvailabilityAccessDeniedError) as create_error:
                create_availability(
                    clinic_id=clinic_id,
                    practitioner_id=rbac_graph.physician,
                    start_local="2035-02-02T10:00",
                    end_local="2035-02-02T11:00",
                    idempotency_key=uuid4(),
                )
            denials.append(create_error.value)
            with pytest.raises(AvailabilityAccessDeniedError) as retire_error:
                retire_availability(
                    clinic_id=clinic_id,
                    availability_id=block.pk,
                )
            denials.append(retire_error.value)
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM clinic_app.audit_event_tenant")
            audit_count = cursor.fetchone()

    assert {error.args for error in denials} == {("availability access denied",)}
    assert audit_count == (1,)


def test_failed_input_and_target_attempts_leave_the_key_reusable(
    rbac_graph: RbacGraph,
) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        Clinic.objects.filter(pk=rbac_graph.clinic_a).update(
            timezone="America/New_York"
        )
    key = uuid4()
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        invalid_intervals = (
            ("2035-05-01T09:00:00", "2035-05-01T10:00"),
            ("2020-05-01T09:00", "2020-05-01T10:00"),
            ("2035-05-01T23:30", "2035-05-02T00:30"),
            ("2035-11-04T01:30", "2035-11-04T02:30"),
        )
        for start_local, end_local in invalid_intervals:
            with pytest.raises(AvailabilityCreateInputError):
                create_availability(
                    clinic_id=rbac_graph.clinic_a,
                    practitioner_id=rbac_graph.physician,
                    start_local=start_local,
                    end_local=end_local,
                    idempotency_key=key,
                )
        with pytest.raises(AvailabilityPractitionerError):
            create_availability(
                clinic_id=rbac_graph.clinic_a,
                practitioner_id=rbac_graph.shared_user,
                start_local="2035-05-01T09:00",
                end_local="2035-05-01T10:00",
                idempotency_key=key,
            )
        accepted = create_availability(
            clinic_id=rbac_graph.clinic_a,
            practitioner_id=rbac_graph.physician,
            start_local="2035-05-01T09:00",
            end_local="2035-05-01T10:00",
            idempotency_key=key,
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM clinic_app.audit_event_tenant")
            audit_count = cursor.fetchone()

    assert accepted.idempotency_key == key
    assert audit_count == (1,)


def test_inactive_missing_and_malformed_current_actors_fail_closed(
    rbac_graph: RbacGraph,
) -> None:
    User.objects.filter(pk=rbac_graph.shared_user).update(is_active=False)
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
        pytest.raises(AvailabilityAccessDeniedError),
    ):
        view_availability(clinic_id=rbac_graph.clinic_a)

    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        for raw_actor in ("", "not-a-uuid"):
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_user_id', %s, true)",
                [raw_actor],
            )
            with pytest.raises(AvailabilityAccessDeniedError):
                view_availability(clinic_id=rbac_graph.clinic_a)
