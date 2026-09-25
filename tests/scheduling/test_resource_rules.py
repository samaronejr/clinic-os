from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.identity.models import Clinic
from apps.scheduling.models import (
    Appointment,
    AppointmentResource,
    AvailabilityBlock,
    AvailabilityTemplate,
    Resource,
)
from apps.scheduling.resource_errors import SchedulingRuleError
from apps.scheduling.resource_services import (
    ClosureInput,
    ResourceInput,
    ServiceInput,
    TemplateInput,
    create_closure,
    create_resource,
    create_service_type,
    create_template,
    generate_availability,
    retire_definition,
)
from apps.scheduling.services import (
    AppointmentAccessDeniedError,
    AppointmentIdempotencyConflictError,
    AppointmentLocalRange,
    ServiceBooking,
    cancel_appointment,
    create_service_appointment,
    prepare_booking,
    reschedule_appointment,
)
from apps.scheduling.timezones import LocalTimeValueError
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone, translation

from patient_service_support import runtime_role
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)
from scheduling.test_resources import _catalog

if TYPE_CHECKING:
    from apps.scheduling.models import ServiceType

    from conftest import RbacGraph
    from scheduling.appointment_service_support import AppointmentSetup

pytestmark = pytest.mark.django_db(transaction=True)


def book(
    setup: AppointmentSetup,
    catalog: tuple[Resource, Resource, ServiceType],
    *,
    start: str = "09:00",
    key: UUID | None = None,
) -> Appointment:
    room, equipment, service = catalog
    local_start = datetime.fromisoformat(f"2035-06-02T{start}+00:00")
    return create_service_appointment(
        clinic_id=setup.clinic_id,
        enrollment_id=setup.enrollment_id,
        practitioner_id=setup.practitioner_id,
        booking=ServiceBooking(
            AppointmentLocalRange(
                local_start.strftime("%Y-%m-%dT%H:%M"),
                (local_start + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M"),
            ),
            service.pk,
            (room.pk, equipment.pk),
        ),
        idempotency_key=key or uuid4(),
    )


def test_selection_preparation_replay_and_terminal_capacity(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    key = uuid4()
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        catalog = _catalog(setup)
        room, equipment, service = catalog
        prepared = prepare_booking(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
            service_type_id=service.pk,
            resource_ids=(equipment.pk, room.pk),
        )
        assert prepared.resource_ids == tuple(sorted((room.pk, equipment.pk)))
        assert (
            prepared.duration_min,
            prepared.buffer_before,
            prepared.buffer_after,
        ) == (30, 10, 10)
        original = book(setup, catalog, key=key)
        assert book(setup, catalog, key=key).pk == original.pk
        with pytest.raises(AppointmentIdempotencyConflictError):
            book(setup, catalog, key=key, start="10:00")
        successor = create_service_type(
            clinic_id=setup.clinic_id,
            content=ServiceInput(
                name="Sintetico successor",
                duration_min=30,
                buffer_before=10,
                buffer_after=10,
                required_resource_kinds=("room", "equipment"),
            ),
        )
        with pytest.raises(AppointmentIdempotencyConflictError):
            book(setup, (room, equipment, successor), key=key)
        moved = reschedule_appointment(
            appointment_id=original.pk,
            local_range=AppointmentLocalRange("2035-06-02T10:00", "2035-06-02T10:30"),
        )
        rows = list(AppointmentResource.objects.filter(appointment=moved))
        assert all(
            row.start_at == moved.start_at - timedelta(minutes=10) for row in rows
        )
        assert all(row.end_at == moved.end_at + timedelta(minutes=10) for row in rows)
        cancel_appointment(appointment_id=original.pk, reason="clinic_request")
        assert not AppointmentResource.objects.filter(
            appointment=original, occupied=True
        ).exists()
        assert AppointmentResource.objects.filter(appointment=original).count() == 2
        assert book(setup, catalog, key=key).status == "cancelled"
        assert book(setup, catalog, start="10:00").pk != original.pk


@pytest.mark.parametrize("subject", ["clinic", "practitioner", "resource"])
def test_holiday_and_absence_refuse_with_translated_closed_reason(
    rbac_graph: RbacGraph, subject: str
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        catalog = _catalog(setup)
        closure = create_closure(
            clinic_id=setup.clinic_id,
            content=ClosureInput(
                start_local="2035-06-02T08:50",
                end_local="2035-06-02T09:00",
                reason="holiday" if subject == "clinic" else "unavailable",
                practitioner_id=setup.practitioner_id
                if subject == "practitioner"
                else None,
                resource_id=catalog[0].pk if subject == "resource" else None,
            ),
        )
        with translation.override("pt-br"), pytest.raises(SchedulingRuleError) as error:
            book(setup, catalog)
        assert error.value.code == "holiday"
        with translation.override("pt-br"):
            assert str(error.value.message) == translation.gettext(
                "This window overlaps a holiday or absence."
            )
        assert Appointment.objects.count() == 0
        retire_definition(
            clinic_id=setup.clinic_id,
            kind="holiday" if subject == "clinic" else "absence",
            record_id=closure.pk,
        )
        assert book(setup, catalog).status == "scheduled"


@pytest.mark.parametrize("start", ["08:00", "11:30"])
def test_buffer_must_fit_practitioner_and_resources(
    rbac_graph: RbacGraph, start: str
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        catalog = _catalog(setup)
        with pytest.raises(SchedulingRuleError) as error:
            book(setup, catalog, start=start)
        assert error.value.code == "outside_template"
        assert Appointment.objects.count() == 0


def test_required_kinds_duration_and_professional_roles(rbac_graph: RbacGraph) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        room, equipment, service = _catalog(setup)
        with pytest.raises(SchedulingRuleError) as error:
            prepare_booking(
                clinic_id=setup.clinic_id,
                enrollment_id=setup.enrollment_id,
                service_type_id=service.pk,
                resource_ids=(room.pk,),
            )
        assert error.value.code == "resource_conflict"
        for selected_service, end in (
            (service, "10:00"),
            (
                create_service_type(
                    clinic_id=setup.clinic_id,
                    content=ServiceInput(
                        name="Sintetico nursing",
                        duration_min=30,
                        required_professional_roles=("nurse",),
                    ),
                ),
                "09:30",
            ),
        ):
            with pytest.raises(SchedulingRuleError) as error:
                create_service_appointment(
                    clinic_id=setup.clinic_id,
                    enrollment_id=setup.enrollment_id,
                    practitioner_id=setup.practitioner_id,
                    booking=ServiceBooking(
                        AppointmentLocalRange("2035-06-02T09:00", f"2035-06-02T{end}"),
                        selected_service.pk,
                        (room.pk, equipment.pk),
                    ),
                    idempotency_key=uuid4(),
                )
            assert error.value.code == "buffer_violation"
        assert Appointment.objects.count() == 0


def test_foreign_and_unknown_resources_have_identical_denial(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(rbac_graph.clinic_admin, setup.organization_id):
        foreign = create_resource(
            clinic_id=rbac_graph.clinic_b,
            content=ResourceInput(name="Sintetico foreign", kind="room"),
        )
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        catalog = _catalog(setup)
        for selector in (foreign.pk, uuid4()):
            with pytest.raises(AppointmentAccessDeniedError) as error:
                prepare_booking(
                    clinic_id=setup.clinic_id,
                    enrollment_id=setup.enrollment_id,
                    service_type_id=catalog[2].pk,
                    resource_ids=(selector,),
                )
            assert str(error.value) == "appointment access denied"
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_b),
    ):
        assert Resource.objects.count() == 0


def test_database_rejects_direct_duration_holiday_and_capacity_tampering(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        catalog = _catalog(setup)
        appointment = book(setup, catalog)
        with pytest.raises(IntegrityError), transaction.atomic():
            Appointment.objects.filter(pk=appointment.pk).update(
                end_at=appointment.end_at + timedelta(minutes=1)
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            AppointmentResource.objects.filter(appointment=appointment).update(
                occupied=False
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            Resource.objects.filter(pk=catalog[0].pk).update(capacity=64)
        with pytest.raises(IntegrityError), transaction.atomic():
            AvailabilityBlock.objects.filter(resource=catalog[0]).update(
                retired_at=timezone.now()
            )
        with pytest.raises(SchedulingRuleError) as error:
            create_closure(
                clinic_id=setup.clinic_id,
                content=ClosureInput(
                    start_local="2035-06-02T09:00",
                    end_local="2035-06-02T09:30",
                    reason="holiday",
                ),
            )
        assert error.value.code == "resource_conflict"
        appointment.refresh_from_db()
        assert appointment.end_at - appointment.start_at == timedelta(minutes=30)
        assert (
            AppointmentResource.objects.filter(
                appointment=appointment, occupied=True
            ).count()
            == 2
        )


def test_reservations_cannot_be_hard_deleted_even_by_owner(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment = book(setup, _catalog(setup))
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        assert AppointmentResource.objects.filter(appointment=appointment).count() == 2
        with pytest.raises(IntegrityError), transaction.atomic():
            cursor.execute(
                "DELETE FROM clinic_app.scheduling_appointmentresource "
                "WHERE appointment_id=%s",
                [appointment.pk],
            )
        assert AppointmentResource.objects.filter(appointment=appointment).count() == 2


def test_practitioner_buffer_protects_legacy_adjacent_bookings(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        service = create_service_type(
            clinic_id=setup.clinic_id,
            content=ServiceInput(
                name="Sintetico buffer", duration_min=30, buffer_after=10
            ),
        )
        first = create_service_appointment(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
            practitioner_id=setup.practitioner_id,
            booking=ServiceBooking(
                AppointmentLocalRange("2035-06-02T09:00", "2035-06-02T09:30"),
                service.pk,
            ),
            idempotency_key=uuid4(),
        )
        with pytest.raises(SchedulingRuleError) as error:
            create_synthetic_appointment(
                setup, start_local="2035-06-02T09:30", end_local="2035-06-02T10:00"
            )
        assert error.value.code == "buffer_violation"
        adjacent = create_synthetic_appointment(
            setup, start_local="2035-06-02T09:40", end_local="2035-06-02T10:10"
        )
        assert adjacent.start_at == first.end_at + timedelta(minutes=10)


def test_definition_and_generation_events_are_once_and_metadata_only(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        room, _, _ = _catalog(setup)
        create_resource(
            clinic_id=setup.clinic_id,
            content=ResourceInput(name="SINTETICO-SENTINELA-RESOURCE", kind="location"),
        )
        template = AvailabilityTemplate.objects.get(resource=room)
        retire_definition(
            clinic_id=setup.clinic_id, kind="template", record_id=template.pk
        )
        retire_definition(
            clinic_id=setup.clinic_id, kind="template", record_id=template.pk
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT event_type,count(*) FROM clinic_app.audit_event_tenant "
                "WHERE event_type LIKE 'scheduling.%' GROUP BY event_type"
            )
            assert dict(cursor.fetchall()) == {
                "scheduling.resource.created": 3,
                "scheduling.service.created": 1,
                "scheduling.template.created": 2,
                "scheduling.availability.created": 3,
                "scheduling.availability.retired": 1,
                "scheduling.definition.retired": 1,
            }
            cursor.execute(
                "SELECT payload FROM clinic_app.audit_event_tenant "
                "WHERE event_type LIKE 'scheduling.%'"
            )
            payloads = [json.loads(row[0]) for row in cursor.fetchall()]
        assert all(set(payload) == {"clinic_id", "object_verb"} for payload in payloads)
        assert all("SINTETICO-SENTINELA" not in str(payload) for payload in payloads)


def test_retired_template_stops_generation_and_future_bookings(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        catalog = _catalog(setup)
        template = AvailabilityTemplate.objects.get(resource=catalog[0])
        retire_definition(
            clinic_id=setup.clinic_id, kind="template", record_id=template.pk
        )
        retire_definition(
            clinic_id=setup.clinic_id, kind="template", record_id=template.pk
        )
        with pytest.raises(SchedulingRuleError) as error:
            generate_availability(
                clinic_id=setup.clinic_id,
                template_id=template.pk,
                start_date=date(2035, 6, 2),
                end_date=date(2035, 6, 2),
            )
        assert error.value.code == "outside_template"
        with pytest.raises(SchedulingRuleError) as error:
            book(setup, catalog)
        assert error.value.code == "outside_template"
        assert (
            AvailabilityBlock.objects.filter(
                template=template, retired_at__isnull=False
            ).count()
            == 1
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            AvailabilityTemplate.objects.filter(pk=template.pk).update(active=True)
        replacement = create_template(
            clinic_id=setup.clinic_id,
            content=TemplateInput(
                resource_id=catalog[0].pk,
                weekdays=(5,),
                start_local=time(8),
                end_local=time(12),
                valid_from=date(2035, 6, 2),
                valid_to=date(2035, 6, 2),
            ),
        )
        generate_availability(
            clinic_id=setup.clinic_id,
            template_id=replacement.pk,
            start_date=date(2035, 6, 2),
            end_date=date(2035, 6, 2),
        )
        assert book(setup, catalog).status == "scheduled"


def test_generation_ignores_clock_and_rejects_dst_gap_atomically(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        assert (
            Clinic.objects.filter(pk=rbac_graph.clinic_a).update(
                timezone="America/New_York"
            )
            == 1
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        room = create_resource(
            clinic_id=rbac_graph.clinic_a,
            content=ResourceInput(name="Sintetico DST", kind="room"),
        )
        template = create_template(
            clinic_id=rbac_graph.clinic_a,
            content=TemplateInput(
                resource_id=room.pk,
                weekdays=(6,),
                start_local=time(2, 30),
                end_local=time(3, 30),
                valid_from=date(2035, 3, 4),
                valid_to=date(2035, 3, 11),
            ),
        )
        with pytest.raises(LocalTimeValueError):
            generate_availability(
                clinic_id=rbac_graph.clinic_a,
                template_id=template.pk,
                start_date=date(2035, 3, 4),
                end_date=date(2035, 3, 11),
            )
        assert not AvailabilityBlock.objects.filter(template=template).exists()
        first = generate_availability(
            clinic_id=rbac_graph.clinic_a,
            template_id=template.pk,
            start_date=date(2035, 3, 4),
            end_date=date(2035, 3, 4),
        )
        monkeypatch.setattr(timezone, "now", lambda: datetime(2040, 1, 1, tzinfo=UTC))
        assert (
            generate_availability(
                clinic_id=rbac_graph.clinic_a,
                template_id=template.pk,
                start_date=date(2035, 3, 4),
                end_date=date(2035, 3, 4),
            )
            == first
        )
        assert AvailabilityBlock.objects.filter(template=template).count() == 1
