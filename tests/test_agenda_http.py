from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

import pytest
from apps.identity.models import UserClinicRole
from apps.scheduling.agenda_presenter import clinic_local_today
from django.utils.translation import gettext

from appointment_http_support import (
    AGENDA_VIEWED_EVENT,
    INSIDE_END,
    INSIDE_START,
    SYNTHETIC_PATIENT,
    BookingContext,
    agenda_at_url,
    agenda_url,
    appointment_create_url,
    create_payload,
    create_zone_clinic,
    local_time_markup,
    seed_appointment,
    seed_enrollment,
)
from availability_http_support import (
    FUTURE_DATE,
    LocalWindow,
    grant_role,
    seed_block,
)
from otp_test_support import runtime_role
from patient_http_support import (
    audit_event_types,
    receptionist_client,
    verified_physician_client,
)

if TYPE_CHECKING:
    from django.test import Client

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

GAP_DATE: Final = "2031-03-09"
FOLD_DATE: Final = "2031-11-02"
SKIPPED_DATE: Final = "2011-12-30"
WEEK_MONDAY: Final = "2031-03-03"
WEEK_SUNDAY: Final = "2031-03-09"
NEXT_MONDAY: Final = "2031-03-10"


def _clinic_with_one_appointment(graph: RbacGraph) -> tuple[Client, BookingContext]:
    client, receptionist = receptionist_client(graph)
    context = BookingContext(graph, receptionist.pk, graph.clinic_a)
    seed_block(graph, receptionist.pk, graph.clinic_a, graph.physician)
    enrollment_id = seed_enrollment(graph, receptionist.pk, graph.clinic_a)
    seed_appointment(context, enrollment_id, graph.physician)
    return client, context


def test_day_agenda_shows_the_local_range_zone_label_and_status_only(
    rbac_graph: RbacGraph,
) -> None:
    client, context = _clinic_with_one_appointment(rbac_graph)

    with runtime_role():
        response = client.get(agenda_at_url(context.clinic_id, "day", FUTURE_DATE))

    body = response.content
    assert response.status_code == 200
    assert SYNTHETIC_PATIENT.encode() in body
    assert local_time_markup(INSIDE_START) in body
    assert local_time_markup(INSIDE_END) in body
    assert b"America/Sao_Paulo" in body
    assert gettext("Scheduled").encode() in body
    assert b"1988-04-05" not in body
    assert audit_event_types(rbac_graph, context.actor).count(AGENDA_VIEWED_EVENT) == 1


def test_default_agenda_uses_the_clinic_local_civil_today(
    rbac_graph: RbacGraph,
) -> None:
    client, context = _clinic_with_one_appointment(rbac_graph)

    with runtime_role():
        response = client.get(agenda_url(context.clinic_id))

    assert response.status_code == 200
    assert b'id="agenda-status"' in response.content
    today = clinic_local_today("America/Sao_Paulo")
    assert f'<time datetime="{today}">'.encode() in response.content
    assert gettext("today").encode() in response.content
    assert audit_event_types(rbac_graph, context.actor).count(AGENDA_VIEWED_EVENT) == 1


def test_week_agenda_uses_the_iso_week_civil_bounds(rbac_graph: RbacGraph) -> None:
    client, context = _clinic_with_one_appointment(rbac_graph)

    with runtime_role():
        inside = client.get(agenda_at_url(context.clinic_id, "week", WEEK_SUNDAY))
        monday = client.get(agenda_at_url(context.clinic_id, "week", WEEK_MONDAY))
        outside = client.get(agenda_at_url(context.clinic_id, "week", NEXT_MONDAY))

    assert [inside.status_code, monday.status_code, outside.status_code] == [200] * 3
    assert SYNTHETIC_PATIENT.encode() in inside.content
    assert SYNTHETIC_PATIENT.encode() in monday.content
    assert SYNTHETIC_PATIENT.encode() not in outside.content


def test_daylight_gap_and_fold_days_carry_their_real_civil_bounds(
    rbac_graph: RbacGraph,
) -> None:
    client, receptionist = receptionist_client(rbac_graph)
    clinic_id = create_zone_clinic(
        rbac_graph, receptionist.pk, rbac_graph.physician, "America/New_York"
    )
    context = BookingContext(rbac_graph, receptionist.pk, clinic_id)
    enrollment_id = seed_enrollment(rbac_graph, receptionist.pk, clinic_id)
    schedule = ((GAP_DATE, ("01:00", "01:45")), (FOLD_DATE, ("00:15", "00:45")))
    for day, window in schedule:
        seed_block(
            rbac_graph,
            receptionist.pk,
            clinic_id,
            rbac_graph.physician,
            LocalWindow(window[0], window[1], day),
        )
        seed_appointment(
            context,
            enrollment_id,
            rbac_graph.physician,
            (f"{day}T{window[0]}", f"{day}T{window[1]}"),
        )

    with runtime_role():
        gap = client.get(agenda_at_url(clinic_id, "day", GAP_DATE))
        fold = client.get(agenda_at_url(clinic_id, "day", FOLD_DATE))

    assert [gap.status_code, fold.status_code] == [200, 200]
    assert local_time_markup(f"{GAP_DATE}T01:00") in gap.content
    assert local_time_markup(f"{FOLD_DATE}T00:15") in fold.content
    assert local_time_markup(f"{FOLD_DATE}T00:15") not in gap.content


def test_a_nonexistent_local_minute_is_never_booked(rbac_graph: RbacGraph) -> None:
    client, receptionist = receptionist_client(rbac_graph)
    clinic_id = create_zone_clinic(
        rbac_graph, receptionist.pk, rbac_graph.physician, "America/New_York"
    )
    enrollment_id = seed_enrollment(rbac_graph, receptionist.pk, clinic_id)

    with runtime_role():
        response = client.post(
            appointment_create_url(clinic_id),
            create_payload(
                enrollment_id,
                rbac_graph.physician,
                f"{GAP_DATE}T02:15",
                f"{GAP_DATE}T02:45",
            ),
        )

    assert response.status_code == 200
    assert b'id="booking-errors"' in response.content


def test_a_completely_skipped_civil_date_renders_an_empty_agenda(
    rbac_graph: RbacGraph,
) -> None:
    client, receptionist = receptionist_client(rbac_graph)
    clinic_id = create_zone_clinic(
        rbac_graph, receptionist.pk, rbac_graph.physician, "Pacific/Apia"
    )

    with runtime_role():
        response = client.get(agenda_at_url(clinic_id, "day", SKIPPED_DATE))

    assert response.status_code == 200
    assert b"0 consulta neste dia." in response.content
    assert b'id="agenda-empty"' in response.content
    assert b'id="agenda-error"' not in response.content


@pytest.mark.parametrize(
    ("view", "day", "page"),
    [("month", FUTURE_DATE, 1), ("day", "2031-13-45", 1), ("day", FUTURE_DATE, 0)],
)
def test_malformed_agenda_input_renders_one_accessible_error(
    rbac_graph: RbacGraph,
    view: str,
    day: str,
    page: int,
) -> None:
    client, context = _clinic_with_one_appointment(rbac_graph)

    with runtime_role():
        response = client.get(agenda_at_url(context.clinic_id, view, day, page))

    assert response.status_code == 200
    assert b'id="agenda-error"' in response.content
    assert SYNTHETIC_PATIENT.encode() not in response.content


def test_physician_sees_only_their_own_rows_without_any_control(
    rbac_graph: RbacGraph,
) -> None:
    _client, context = _clinic_with_one_appointment(rbac_graph)
    physician = verified_physician_client(rbac_graph)

    with runtime_role():
        response = physician.get(
            agenda_at_url(context.clinic_id, "day", FUTURE_DATE),
        )

    body = response.content
    assert response.status_code == 200
    assert SYNTHETIC_PATIENT.encode() in body
    # The only controls are the physician's own encounter and questionnaire
    # actions; reception's scheduling controls never render.
    actions = re.findall(rb'<button[^>]*name="action" value="([^"]+)"', body)
    assert actions
    assert all(value in {b"open", b"appointment"} for value in actions)
    assert b"/reschedule/" not in body
    assert b"/cancel/" not in body
    assert b"1988-04-05" not in body


def test_agenda_refuses_a_foreign_or_unknown_clinic(rbac_graph: RbacGraph) -> None:
    client, context = _clinic_with_one_appointment(rbac_graph)
    del context

    with runtime_role():
        foreign = client.get(agenda_url(rbac_graph.clinic_b))
        unknown = client.get(agenda_url(rbac_graph.organization_b))

    assert {foreign.status_code, unknown.status_code} == {404}


def test_agenda_responses_are_private_and_uncacheable(
    rbac_graph: RbacGraph,
) -> None:
    client, context = _clinic_with_one_appointment(rbac_graph)

    with runtime_role():
        response = client.get(agenda_url(context.clinic_id))

    cache_control = response.headers["Cache-Control"]
    for directive in ("private", "no-store", "no-cache", "must-revalidate"):
        assert directive in cache_control
    assert "HX-Request" in response.headers["Vary"]


def test_agenda_paginates_without_any_query_string(rbac_graph: RbacGraph) -> None:
    client, receptionist = receptionist_client(rbac_graph)
    context = BookingContext(rbac_graph, receptionist.pk, rbac_graph.clinic_a)
    seed_block(
        rbac_graph,
        receptionist.pk,
        rbac_graph.clinic_a,
        rbac_graph.physician,
        LocalWindow("08:00", "15:00"),
    )
    for index in range(26):
        enrollment_id = seed_enrollment(
            rbac_graph,
            receptionist.pk,
            rbac_graph.clinic_a,
            f"Patient {index:02d} Synthetic Testpatient",
            1 + index % 27,
        )
        start = f"{FUTURE_DATE}T{8 + index // 6:02d}:{(index % 6) * 10:02d}"
        end = f"{FUTURE_DATE}T{8 + index // 6:02d}:{(index % 6) * 10 + 5:02d}"
        seed_appointment(context, enrollment_id, rbac_graph.physician, (start, end))

    with runtime_role():
        first = client.get(agenda_at_url(rbac_graph.clinic_a, "day", FUTURE_DATE, 1))
        second = client.get(agenda_at_url(rbac_graph.clinic_a, "day", FUTURE_DATE, 2))

    assert [first.status_code, second.status_code] == [200, 200]
    assert "Página 1 de 2".encode() in first.content
    assert "Página 2 de 2".encode() in second.content
    assert agenda_at_url(rbac_graph.clinic_a, "day", FUTURE_DATE, 2).encode() in (
        first.content
    )
    assert b"?" not in first.content.split(b'class="agenda-pagination"')[1][:400]


def test_agenda_is_readable_by_a_second_clinic_manager_only_for_its_own_clinic(
    rbac_graph: RbacGraph,
) -> None:
    client, context = _clinic_with_one_appointment(rbac_graph)
    grant_role(
        rbac_graph,
        context.actor,
        rbac_graph.clinic_b,
        UserClinicRole.Role.RECEPTIONIST,
    )

    with runtime_role():
        response = client.get(agenda_url(rbac_graph.clinic_b))

    assert response.status_code == 200
    assert SYNTHETIC_PATIENT.encode() not in response.content
