"""Todo 50 portal booking by service type over the todo 21 resource model.

Written test-first; its red run is in the todo 50 evidence. The portal adds the
patient service projection to ``apps.scheduling.patient_booking``:
``PatientService``, ``patient_services()`` and
``patient_slots(day, appointment_id=None, *, service_type_id=...)``, whose
signed slot token binds the service and the free resources the slot found.
Booking still goes through ``book_patient_slot`` and the shared
``_create_appointment`` path, so todo 21's ``booking_selection``,
``validate_resource_window`` and the capacity guard's per-resource units decide
the write; nothing here duplicates those checks in Python. The screen takes
the service choice in a POST body (``action=slots``), never in a URL.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from html.parser import HTMLParser
from threading import Barrier
from typing import TYPE_CHECKING, Final
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from apps.identity.models import User
from apps.intake.patient_access import patient_session_context
from apps.scheduling.models import (
    Appointment,
    AppointmentResource,
    PatientBookingEvent,
)
from apps.scheduling.patient_authority import require_patient_booking_scope
from apps.scheduling.patient_booking import (
    PatientService,
    book_patient_slot,
    patient_services,
    patient_slots,
)
from apps.scheduling.resource_errors import SchedulingRuleError
from apps.scheduling.resource_services import (
    ResourceInput,
    ServiceInput,
    TemplateInput,
    create_resource,
    create_service_type,
    create_template,
    generate_availability,
    retire_definition,
)
from apps.scheduling.services import AppointmentAccessDeniedError
from apps.tenancy.db import tenant_context
from django.db import connection, connections

from patient_service_support import runtime_role
from renewal.test_self_booking import DAY, URL, _client, _session
from scheduling.appointment_service_support import (
    seed_appointment_setup,
    seed_cross_clinic_appointment_setups,
)
from scheduling.resource_test_support import independent_setup

if TYPE_CHECKING:
    from apps.scheduling.models import Resource, ServiceType
    from apps.scheduling.patient_booking import PatientSlot

    from conftest import RbacGraph
    from scheduling.appointment_service_support import AppointmentSetup

pytestmark = pytest.mark.django_db(transaction=True)
SERVICE_NAME: Final = "Sintetico consulta com sala do portal"
DURATION: Final = timedelta(minutes=30)
BUFFER: Final = timedelta(minutes=10)
OPENING: Final = time(8)
CLOSING: Final = time(12)


def _catalog(setup: AppointmentSetup) -> tuple[Resource, ServiceType]:
    room = create_resource(
        clinic_id=setup.clinic_id,
        content=ResourceInput(name="Sintetico sala do portal", kind="room"),
    )
    service = create_service_type(
        clinic_id=setup.clinic_id,
        content=ServiceInput(
            name=SERVICE_NAME,
            duration_min=30,
            buffer_before=10,
            buffer_after=10,
            required_resource_kinds=("room",),
        ),
    )
    template = create_template(
        clinic_id=setup.clinic_id,
        content=TemplateInput(
            resource_id=room.pk,
            weekdays=(DAY.weekday(),),
            start_local=OPENING,
            end_local=CLOSING,
            valid_from=DAY,
            valid_to=DAY,
        ),
    )
    generate_availability(
        clinic_id=setup.clinic_id,
        template_id=template.pk,
        start_date=DAY,
        end_date=DAY,
    )
    return room, service


def _local(moment: time) -> datetime:
    zone = ZoneInfo(require_patient_booking_scope().timezone)
    return datetime.combine(DAY, moment, tzinfo=zone)


def _buffered_windows_apart(slot: PatientSlot, appointment: Appointment) -> bool:
    return (
        slot.end_at + BUFFER <= appointment.start_at - BUFFER
        or slot.start_at - BUFFER >= appointment.end_at + BUFFER
    )


class _Attributes(HTMLParser):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self.values: list[str] = []
        self.targets: list[str] = []
        self.slot_tokens: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        fields = dict(attrs)
        if tag == "input" and fields.get("name") == "slot":
            self.slot_tokens.append(fields.get("value") or "")
        for key, value in attrs:
            if key == self.name:
                self.values.append(value or "")
            if key in {"href", "action", "hx-get", "hx-post"} and value:
                self.targets.append(value)


def _parse(content: bytes, name: str) -> _Attributes:
    parser = _Attributes(name)
    parser.feed(content.decode())
    return parser


def test_patient_sees_only_active_services_of_the_session_clinic(
    rbac_graph: RbacGraph,
) -> None:
    first, second = seed_cross_clinic_appointment_setups(rbac_graph)
    with runtime_role(), tenant_context(first.actor_id, first.organization_id):
        _, service = _catalog(first)
        retired = create_service_type(
            clinic_id=first.clinic_id,
            content=ServiceInput(name="Sintetico servico retirado", duration_min=30),
        )
        retire_definition(
            clinic_id=first.clinic_id, kind="service", record_id=retired.pk
        )
        foreign = create_service_type(
            clinic_id=second.clinic_id,
            content=ServiceInput(
                name="Sintetico servico de outra clinica", duration_min=30
            ),
        )
    session_id = _session(first)
    with runtime_role(), patient_session_context(session_id):
        assert patient_services() == (
            PatientService(
                service_type_id=service.pk, name=SERVICE_NAME, duration_min=30
            ),
        )
        for selector in (retired.pk, foreign.pk, uuid4()):
            with pytest.raises(AppointmentAccessDeniedError):
                patient_slots(DAY, service_type_id=selector)
        assert Appointment.objects.count() == 0
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT NULLIF(current_setting('app.current_tenant',true),''), "
                "NULLIF(current_setting('app.current_user_id',true),'')"
            )
            assert cursor.fetchone() == (None, None)


def test_service_slots_bind_duration_buffers_and_the_free_room(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        room, service = _catalog(setup)
    session_id = _session(setup)
    with runtime_role(), patient_session_context(session_id):
        slots = patient_slots(DAY, service_type_id=service.pk)
        assert slots
        for slot in slots:
            assert slot.end_at - slot.start_at == DURATION
            assert slot.start_at - BUFFER >= _local(OPENING)
            assert slot.end_at + BUFFER <= _local(CLOSING)
        booked = book_patient_slot(token=slots[0].token, idempotency_key=uuid4())
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment = Appointment.objects.get(pk=booked.pk)
        assert appointment.service_type_id == service.pk
        assert (appointment.buffer_before, appointment.buffer_after) == (10, 10)
        assert list(
            AppointmentResource.objects.filter(appointment=appointment).values_list(
                "resource_id", flat=True
            )
        ) == [room.pk]
        assert list(
            PatientBookingEvent.objects.filter(
                appointment_id=appointment.pk
            ).values_list("action", flat=True)
        ) == ["created"]
    other_session = _session(independent_setup(setup, 1))
    with runtime_role(), patient_session_context(other_session):
        remaining = patient_slots(DAY, service_type_id=service.pk)
        assert remaining
        assert all(_buffered_windows_apart(slot, appointment) for slot in remaining)


def test_service_without_a_free_required_resource_offers_no_slot(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        _catalog(setup)
        scan = create_service_type(
            clinic_id=setup.clinic_id,
            content=ServiceInput(
                name="Sintetico exame sem equipamento",
                duration_min=30,
                required_resource_kinds=("equipment",),
            ),
        )
    session_id = _session(setup)
    with runtime_role(), patient_session_context(session_id):
        assert patient_slots(DAY, service_type_id=scan.pk) == ()
        assert Appointment.objects.count() == 0


def test_two_patients_racing_for_the_last_room_get_one_booking(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        _, service = _catalog(setup)
    other = independent_setup(setup, 1)
    targets = (setup, other)
    labels = dict(
        User.objects.filter(
            pk__in=[target.practitioner_id for target in targets]
        ).values_list("pk", "username")
    )
    sessions = tuple(_session(target) for target in targets)
    chosen: list[PatientSlot] = []
    for session_id, target in zip(sessions, targets, strict=True):
        with runtime_role(), patient_session_context(session_id):
            own = [
                slot
                for slot in patient_slots(DAY, service_type_id=service.pk)
                if slot.practitioner == labels[target.practitioner_id]
            ]
            chosen.append(min(own, key=lambda slot: slot.start_at))
    assert chosen[0].start_at == chosen[1].start_at
    barrier = Barrier(2, timeout=30)

    def book(index: int) -> str:
        connections.close_all()
        try:
            with runtime_role(), patient_session_context(sessions[index]):
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL statement_timeout='30s'")
                barrier.wait()
                try:
                    book_patient_slot(
                        token=chosen[index].token, idempotency_key=uuid4()
                    )
                except SchedulingRuleError as error:
                    return error.code
                return "booked"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(book, range(2), timeout=45))
    assert sorted(results) == ["booked", "resource_conflict"]
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert Appointment.objects.count() == 1
        assert AppointmentResource.objects.count() == 1


def test_booking_screen_takes_the_service_choice_in_the_post_body(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        room, service = _catalog(setup)
    client = _client(_session(setup))
    selector = str(service.pk)
    with runtime_role():
        screen = client.get(URL)
        assert screen.status_code == 200
        assert _parse(screen.content, "data-service-option").values == [selector]
        chosen = client.post(
            URL,
            {"action": "slots", "day": DAY.isoformat(), "service_type_id": selector},
        )
        assert chosen.status_code == 200
        page = _parse(chosen.content, "data-slot-service")
        assert page.values
        assert set(page.values) == {selector}
        assert all(selector not in target for target in page.targets)
        tokens = page.slot_tokens
        assert len(tokens) == len(page.values)
        booked = client.post(
            URL,
            {
                "action": "book",
                "slot": tokens[0],
                "idempotency_key": str(uuid4()),
                "day": DAY.isoformat(),
            },
        )
        assert booked.status_code == 303
        assert selector not in booked["Location"]
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert list(
            AppointmentResource.objects.values_list("resource_id", flat=True)
        ) == [room.pk]
