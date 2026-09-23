from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

import pytest

from otp_test_support import runtime_role
from patient_http_support import (
    receptionist_client,
    verified_physician_client,
)
from scheduling.availability_http_support import (
    ADJACENT,
    availability_list_url,
    create_dst_clinic,
    seed_block,
)

if TYPE_CHECKING:
    from uuid import UUID

    from django.test import Client

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

FORM_ACTION: Final = re.compile(rb'<form[^>]*action="([^"]*)"')
PAST_DATE: Final = "2020-05-06"


def _manager(graph: RbacGraph) -> tuple[Client, UUID, str]:
    client, receptionist = receptionist_client(graph)
    return client, receptionist.pk, availability_list_url(graph.clinic_a)


def test_manager_get_offers_resolver_practitioner_choices(
    rbac_graph: RbacGraph,
) -> None:
    client, _actor, url = _manager(rbac_graph)

    with runtime_role():
        response = client.get(url)

    assert response.status_code == 200
    assert str(rbac_graph.physician).encode() in response.content
    assert str(rbac_graph.clinic_admin).encode() not in response.content
    assert str(rbac_graph.shared_user).encode() not in response.content


def test_manager_get_never_lists_a_foreign_clinic_window(
    rbac_graph: RbacGraph,
) -> None:
    client, actor, url = _manager(rbac_graph)
    seed_block(rbac_graph, actor, rbac_graph.clinic_a, rbac_graph.physician)
    foreign = create_dst_clinic(rbac_graph, actor, rbac_graph.physician)
    seed_block(rbac_graph, actor, foreign, rbac_graph.physician, ADJACENT)

    with runtime_role():
        response = client.get(url)

    assert response.status_code == 200
    assert response.content.count(b"<tbody>") == 1
    assert b"08:00" in response.content
    assert b"09:00</td>" in response.content
    assert response.content.count(b'<th scope="row">') == 1


def test_physician_sees_only_own_windows_without_management_controls(
    rbac_graph: RbacGraph,
) -> None:
    _client, actor, _url = _manager(rbac_graph)
    seed_block(rbac_graph, actor, rbac_graph.clinic_a, rbac_graph.physician)
    physician = verified_physician_client(rbac_graph)

    with runtime_role():
        response = physician.get(availability_list_url(rbac_graph.clinic_a))

    assert response.status_code == 200
    assert b"Add an availability window" not in response.content
    assert b"Retire the" not in response.content
    assert b"08:00" in response.content
