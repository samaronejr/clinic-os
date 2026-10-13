from __future__ import annotations

import io
from dataclasses import replace
from datetime import date, time
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import RoleGrant, UserClinicRole
from apps.scheduling.models import Appointment, AvailabilityBlock, AvailabilityTemplate
from apps.scheduling.resource_services import (
    ResourceInput,
    ServiceInput,
    TemplateInput,
    create_resource,
    create_service_type,
    create_template,
    generate_availability,
)
from apps.scheduling.services import (
    AppointmentAccessDeniedError,
    AppointmentLocalRange,
    AppointmentPractitionerError,
    cancel_appointment,
    reschedule_appointment,
)
from apps.scheduling.timezones import (
    ClinicTimezoneLockedError,
    ensure_clinic_timezone_change_allowed,
    parse_local_minute,
)
from apps.tenancy.db import tenant_context
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError
from django.utils import timezone
from psycopg.errors import CheckViolation, InsufficientPrivilege

from identity.permission_support import owner_context, permission_actor
from patient_service_support import runtime_role
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)
from scheduling.test_resource_rules import book
from scheduling.test_resources import _catalog

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("resource_clock"),
]


def test_narrowed_scheduling_permission_blocks_configuration(
    rbac_graph: RbacGraph,
) -> None:
    actor, _ = permission_actor(rbac_graph, "scheduler")
    with owner_context(rbac_graph.organization_a):
        RoleGrant.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            role="scheduler",
            permission="appointment.book",
            valid_from=timezone.now(),
        )
    with (
        runtime_role(),
        tenant_context(actor, rbac_graph.organization_a),
        pytest.raises(AppointmentAccessDeniedError),
    ):
        create_resource(
            clinic_id=rbac_graph.clinic_a,
            content=ResourceInput(name="Sintetico narrowed", kind="room"),
        )


def test_physician_books_moves_and_cancels_only_own_service_bookings(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        catalog = _catalog(setup)
    with runtime_role(), tenant_context(setup.practitioner_id, setup.organization_id):
        appointment = book(setup, catalog)
        reschedule_appointment(
            appointment_id=appointment.pk,
            local_range=AppointmentLocalRange("2035-06-02T10:00", "2035-06-02T10:30"),
        )
        assert (
            cancel_appointment(
                appointment_id=appointment.pk, reason="clinic_request"
            ).status
            == "cancelled"
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, setup.organization_id),
        pytest.raises(AppointmentAccessDeniedError),
    ):
        book(setup, catalog)


def test_service_roles_support_nurse_templates_without_widening_legacy_booking(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    nurse, _ = permission_actor(rbac_graph, "nurse")
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        room, equipment, _ = _catalog(setup)
        service = create_service_type(
            clinic_id=setup.clinic_id,
            content=ServiceInput(
                name="Sintetico nursing service",
                duration_min=30,
                required_professional_roles=("nurse",),
                required_resource_kinds=("room", "equipment"),
            ),
        )
        template = create_template(
            clinic_id=setup.clinic_id,
            content=TemplateInput(
                practitioner_id=nurse,
                weekdays=(5,),
                start_local=time(8),
                end_local=time(12),
                valid_from=date(2035, 6, 2),
                valid_to=date(2035, 6, 2),
            ),
        )
        generate_availability(
            clinic_id=setup.clinic_id,
            template_id=template.pk,
            start_date=date(2035, 6, 2),
            end_date=date(2035, 6, 2),
        )
        appointment = book(
            replace(setup, practitioner_id=nurse), (room, equipment, service)
        )
        assert appointment.practitioner_id == nurse
        with pytest.raises(AppointmentPractitionerError):
            create_synthetic_appointment(
                replace(setup, practitioner_id=nurse),
                start_local="2035-06-02T10:00",
                end_local="2035-06-02T11:00",
            )


def test_definition_guard_rejects_a_retained_revoked_practitioner(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    content = {
        "organization_id": setup.organization_id,
        "clinic_id": setup.clinic_id,
        "practitioner_id": setup.practitioner_id,
        "weekdays": [5],
        "start_local": time(8),
        "end_local": time(12),
        "valid_from": date(2035, 6, 2),
        "valid_to": date(2035, 6, 2),
        "timezone": "America/Sao_Paulo",
    }
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert AvailabilityTemplate.objects.create(**content).practitioner_id == (
            setup.practitioner_id
        )
    with owner_context(setup.organization_id):
        assert (
            UserClinicRole.objects.filter(
                clinic_id=setup.clinic_id,
                user_id=setup.practitioner_id,
                role=UserClinicRole.Role.PHYSICIAN,
            ).update(revoked_at=timezone.now())
            == 1
        )
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        with pytest.raises(DatabaseError, match="scheduling access denied") as raised:
            AvailabilityTemplate.objects.create(**content)
        assert isinstance(raised.value.__cause__, InsufficientPrivilege)


def test_capacity_guard_rejects_a_retained_revoked_practitioner(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        room = create_resource(
            clinic_id=setup.clinic_id,
            content=ResourceInput(name="Synthetic current-member room", kind="room"),
        )
        service = create_service_type(
            clinic_id=setup.clinic_id,
            content=ServiceInput(
                name="Synthetic current-member service",
                duration_min=30,
                required_resource_kinds=("room",),
                required_professional_roles=("physician",),
            ),
        )
        template = create_template(
            clinic_id=setup.clinic_id,
            content=TemplateInput(
                resource_id=room.pk,
                weekdays=(5,),
                start_local=time(8),
                end_local=time(12),
                valid_from=date(2035, 6, 2),
                valid_to=date(2035, 6, 2),
            ),
        )
        generate_availability(
            clinic_id=setup.clinic_id,
            template_id=template.pk,
            start_date=date(2035, 6, 2),
            end_date=date(2035, 6, 2),
        )
        assert Appointment.objects.create(
            organization_id=setup.organization_id,
            clinic_id=setup.clinic_id,
            patient_id=setup.patient_id,
            practitioner_id=setup.practitioner_id,
            service_type=service,
            resource_ids=[room.pk],
            start_at=parse_local_minute("2035-06-02T09:00", "America/Sao_Paulo"),
            end_at=parse_local_minute("2035-06-02T09:30", "America/Sao_Paulo"),
            idempotency_key=uuid4(),
            create_fingerprint=b"s" * 32,
        ).pk
    with owner_context(setup.organization_id):
        assert (
            UserClinicRole.objects.filter(
                clinic_id=setup.clinic_id,
                user_id=setup.practitioner_id,
                role=UserClinicRole.Role.PHYSICIAN,
            ).update(revoked_at=timezone.now())
            == 1
        )
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        with pytest.raises(
            IntegrityError, match="professional role unavailable"
        ) as raised:
            Appointment.objects.create(
                organization_id=setup.organization_id,
                clinic_id=setup.clinic_id,
                patient_id=setup.patient_id,
                practitioner_id=setup.practitioner_id,
                service_type=service,
                resource_ids=[room.pk],
                start_at=parse_local_minute("2035-06-02T10:00", "America/Sao_Paulo"),
                end_at=parse_local_minute("2035-06-02T10:30", "America/Sao_Paulo"),
                idempotency_key=uuid4(),
                create_fingerprint=b"s" * 32,
            )
        assert isinstance(raised.value.__cause__, CheckViolation)
        assert (
            raised.value.__cause__.diag.constraint_name == "scheduling_buffer_violation"
        )


def test_generation_command_is_idempotent_and_template_freezes_timezone(
    rbac_graph: RbacGraph,
) -> None:
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        resource = create_resource(
            clinic_id=rbac_graph.clinic_a,
            content=ResourceInput(name="Sintetico job", kind="room"),
        )
        template = create_template(
            clinic_id=rbac_graph.clinic_a,
            content=TemplateInput(
                resource_id=resource.pk,
                weekdays=(5,),
                start_local=time(8),
                end_local=time(12),
                valid_from=date(2035, 6, 2),
                valid_to=date(2035, 6, 2),
            ),
        )
        with pytest.raises(ClinicTimezoneLockedError):
            ensure_clinic_timezone_change_allowed(rbac_graph.clinic_a)
    for _ in range(2):
        output = io.StringIO()
        with runtime_role():
            call_command(
                "generate_scheduling_availability",
                "--user-id",
                str(rbac_graph.shared_user),
                "--organization-id",
                str(rbac_graph.organization_a),
                "--clinic-id",
                str(rbac_graph.clinic_a),
                "--template-id",
                str(template.pk),
                "--start-date",
                "2035-06-02",
                "--end-date",
                "2035-06-02",
                stdout=output,
            )
        assert output.getvalue() == "generated_blocks=1\n"
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        assert AvailabilityBlock.objects.filter(template=template).count() == 1
        with pytest.raises(AppointmentAccessDeniedError):
            create_template(
                clinic_id=rbac_graph.clinic_a,
                content=TemplateInput(
                    resource_id=uuid4(),
                    weekdays=(5,),
                    start_local=time(8),
                    end_local=time(12),
                    valid_from=date(2035, 6, 2),
                    valid_to=date(2035, 6, 2),
                ),
            )
