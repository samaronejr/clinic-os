from __future__ import annotations

import re
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from apps.identity.models import User
from django.contrib.staticfiles import finders
from django.utils.translation import gettext

from accessible_document import Document
from availability_http_support import (
    FUTURE_DATE,
    MORNING,
    LocalWindow,
    active_block_ids,
    availability_list_url,
    availability_retire_url,
    book_inside_block,
    create_form_payload,
    seed_block,
)
from otp_test_support import runtime_role
from patient_http_support import receptionist_client, verified_physician_client

if TYPE_CHECKING:
    from django.test import Client

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _manager(rbac_graph: RbacGraph) -> tuple[Client, UUID]:
    client, receptionist = receptionist_client(rbac_graph)
    return client, receptionist.pk


def test_blank_manager_screen_meets_the_dom_accessibility_contract(
    rbac_graph: RbacGraph,
) -> None:
    client, _actor = _manager(rbac_graph)

    with runtime_role():
        response = client.get(availability_list_url(rbac_graph.clinic_a))

    document = Document(response.content)
    assert response.status_code == 200
    assert len(document.tagged("h1")) == 1
    document.assert_unique_identifiers()
    document.assert_descriptions_resolve()
    document.assert_every_control_is_labelled()
    document.assert_every_form_is_post_with_csrf()
    assert document.attributes_for("id_practitioner")["aria-describedby"] == (
        "availability-practitioner-help"
    )
    assert b'<a class="skip-link" href="#main-content">' in response.content


def test_blank_manager_screen_issues_one_fresh_idempotency_key(
    rbac_graph: RbacGraph,
) -> None:
    client, _actor = _manager(rbac_graph)
    url = availability_list_url(rbac_graph.clinic_a)

    with runtime_role():
        first = Document(client.get(url).content)
        second = Document(client.get(url).content)

    keys = [
        document.attributes_for("id_idempotency_key")["value"]
        for document in (first, second)
    ]
    assert all(key and UUID(key) for key in keys)
    assert keys[0] != keys[1]


def test_populated_manager_screen_exposes_an_accessible_table(
    rbac_graph: RbacGraph,
) -> None:
    client, actor = _manager(rbac_graph)
    seed_block(rbac_graph, actor, rbac_graph.clinic_a, rbac_graph.physician)

    with runtime_role():
        response = client.get(availability_list_url(rbac_graph.clinic_a))

    document = Document(response.content)
    document.assert_unique_identifiers()
    document.assert_descriptions_resolve()
    document.assert_every_form_is_post_with_csrf()
    physician = User.objects.get(pk=rbac_graph.physician).username
    # One ledger per physician: the caption names the physician, the
    # keyboard-focusable scroll region is labelled by it, and each
    # clinic-local day opens a row group before its start/end rows.
    assert f'<caption id="availability-caption-1">{physician} '.encode() in (
        response.content
    )
    region = document.tagged("div")
    assert any(
        attributes.get("role") == "region"
        and attributes.get("aria-labelledby") == "availability-caption-1"
        and attributes.get("tabindex") == "0"
        for attributes in region
    )
    assert document.attributes_for("availability-status")["role"] == "status"
    headers = document.tagged("th")
    assert {attributes.get("scope") for attributes in headers} == {
        "col",
        "row",
        "rowgroup",
    }
    assert b'<time datetime="2031-03-04">' in response.content
    assert b"04/03/2031</time></th>" in response.content
    # The row action keeps a short visible verb and a distinct accessible name.
    action = re.search(
        rb'<button class="button button--secondary" type="submit">(.*?)</button>',
        response.content,
        re.DOTALL,
    )
    assert action is not None
    label = action.group(1).decode()
    assert label.startswith(gettext("Retire period"))
    assert '<span class="visually-hidden">' in label
    for fragment in ("08:00", "09:00", "04/03/2031", physician):
        assert fragment in label


def test_bound_create_errors_are_described_and_announced(
    rbac_graph: RbacGraph,
) -> None:
    client, _actor = _manager(rbac_graph)

    with runtime_role():
        response = client.post(
            availability_list_url(rbac_graph.clinic_a),
            create_form_payload(rbac_graph.physician, LocalWindow("", "", "")),
        )

    document = Document(response.content)
    assert response.status_code == 200
    assert document.attributes_for("id_local_date")["aria-describedby"] == (
        "availability-date-help id_local_date_error"
    )
    assert document.attributes_for("scheduling-errors")["role"] == "alert"
    document.assert_unique_identifiers()
    document.assert_descriptions_resolve()
    document.assert_every_control_is_labelled()


def test_refused_retirement_announces_an_accessible_alert(
    rbac_graph: RbacGraph,
) -> None:
    client, actor = _manager(rbac_graph)
    block = seed_block(rbac_graph, actor, rbac_graph.clinic_a, rbac_graph.physician)
    book_inside_block(
        rbac_graph, actor, rbac_graph.clinic_a, rbac_graph.physician, MORNING
    )

    with runtime_role():
        response = client.post(
            availability_retire_url(rbac_graph.clinic_a, block.pk),
            headers={"hx-request": "true"},
        )

    document = Document(response.content)
    assert response.status_code == 200
    assert document.attributes_for("scheduling-retire-error")["role"] == "alert"
    assert document.attributes_for("scheduling-retire-error")["tabindex"] == "-1"
    document.assert_unique_identifiers()
    document.assert_descriptions_resolve()
    assert b"<html" not in response.content.lower()
    # The refused row is the one marked, and it says why in words.
    assert response.content.count(b"table-row--error") == 1
    assert gettext("Has appointments").encode() in response.content
    assert b"08:00" in response.content


def test_refused_retirement_native_page_keeps_a_fresh_create_key(
    rbac_graph: RbacGraph,
) -> None:
    client, actor = _manager(rbac_graph)
    block = seed_block(rbac_graph, actor, rbac_graph.clinic_a, rbac_graph.physician)
    book_inside_block(
        rbac_graph, actor, rbac_graph.clinic_a, rbac_graph.physician, MORNING
    )

    with runtime_role():
        response = client.post(availability_retire_url(rbac_graph.clinic_a, block.pk))

    document = Document(response.content)
    assert response.status_code == 200
    assert document.attributes_for("scheduling-retire-error")["role"] == "alert"
    key = document.attributes_for("id_idempotency_key")["value"]
    assert key
    assert UUID(key)
    document.assert_unique_identifiers()
    document.assert_every_form_is_post_with_csrf()


def test_completed_create_and_retirement_show_one_success_notice_each(
    rbac_graph: RbacGraph,
) -> None:
    client, actor = _manager(rbac_graph)
    url = availability_list_url(rbac_graph.clinic_a)

    with runtime_role():
        created = client.post(url, create_form_payload(rbac_graph.physician, MORNING))
        after_create = client.get(url)
        reloaded = client.get(url)
        block_id = active_block_ids(rbac_graph, actor, rbac_graph.clinic_a)[0]
        retired = client.post(availability_retire_url(rbac_graph.clinic_a, block_id))
        after_retire = client.get(url)

    assert created.status_code == 303
    assert retired.status_code == 303
    created_document = Document(after_create.content)
    assert created_document.attributes_for("availability-created")["role"] == ("status")
    assert b'id="availability-retired"' not in after_create.content
    assert b'id="availability-created"' not in reloaded.content
    retired_document = Document(after_retire.content)
    assert retired_document.attributes_for("availability-retired")["role"] == ("status")
    assert gettext("Period retired").encode() in after_retire.content
    assert (
        gettext(
            "No appointment was cancelled. To offer these hours again, "
            "add a new period."
        ).encode()
        in after_retire.content
    )
    assert str(block_id).encode() not in after_retire.content


def test_physician_screen_is_readable_and_offers_no_write_control(
    rbac_graph: RbacGraph,
) -> None:
    _client, actor = _manager(rbac_graph)
    seed_block(rbac_graph, actor, rbac_graph.clinic_a, rbac_graph.physician)
    physician = verified_physician_client(rbac_graph)

    with runtime_role():
        response = physician.get(availability_list_url(rbac_graph.clinic_a))

    document = Document(response.content)
    assert response.status_code == 200
    assert len(document.tagged("h1")) == 1
    assert document.tagged("form") == []
    assert document.tagged("button") == []
    document.assert_unique_identifiers()
    document.assert_descriptions_resolve()
    assert f'<time datetime="{FUTURE_DATE}">'.encode() in response.content
    assert b"04/03/2031</time></th>" in response.content
    assert {attributes.get("scope") for attributes in document.tagged("th")} == {
        "col",
        "row",
        "rowgroup",
    }


def test_scheduling_stylesheet_is_self_hosted_and_discoverable() -> None:
    assert finders.find("css/clinic-os-scheduling.css") is not None
