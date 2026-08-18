from __future__ import annotations

from collections import Counter
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Final

import pytest
from django.contrib.staticfiles import finders

from otp_test_support import runtime_role
from patient_http_support import (
    patient_create_url,
    patient_list_url,
    receptionist_client,
    seed_patients,
)

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

LABELABLE: Final = frozenset({"input", "select", "textarea"})
UNLABELLED_INPUT_TYPES: Final = frozenset({"hidden", "submit", "button"})
SEEDED: Final = tuple(f"Marina Synthetic P{index:03d}" for index in range(30))


class _Document(HTMLParser):
    def __init__(self, content: bytes) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.labels: list[dict[str, str | None]] = []
        self.feed(content.decode())

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        if tag == "label":
            self.labels.append(attributes)

    def identifiers(self) -> list[str]:
        return [
            element_id
            for _tag, attributes in self.elements
            if (element_id := attributes.get("id")) is not None
        ]

    def attributes_for(self, element_id: str) -> dict[str, str | None]:
        for _tag, attributes in self.elements:
            if attributes.get("id") == element_id:
                return attributes
        pytest.fail(f"missing element #{element_id}")

    def tagged(self, tag: str) -> list[dict[str, str | None]]:
        return [attributes for name, attributes in self.elements if name == tag]

    def assert_unique_identifiers(self) -> None:
        duplicates = [
            value for value, count in Counter(self.identifiers()).items() if count > 1
        ]
        assert duplicates == []

    def assert_descriptions_resolve(self) -> None:
        known = set(self.identifiers())
        for _tag, attributes in self.elements:
            described_by = attributes.get("aria-describedby")
            if described_by is not None:
                assert set(described_by.split()) <= known
            labelled_by = attributes.get("aria-labelledby")
            if labelled_by is not None:
                assert set(labelled_by.split()) <= known

    def assert_every_control_is_labelled(self) -> None:
        label_targets = {
            target for label in self.labels if (target := label.get("for")) is not None
        }
        for tag, attributes in self.elements:
            if tag not in LABELABLE:
                continue
            if attributes.get("type") in UNLABELLED_INPUT_TYPES:
                continue
            element_id = attributes.get("id")
            assert element_id is not None
            assert element_id in label_targets

    def assert_every_form_is_post_with_csrf(self) -> None:
        forms = self.tagged("form")
        assert forms
        for form in forms:
            assert (form.get("method") or "").lower() == "post"
            action = form.get("action") or ""
            assert "?" not in action
        tokens = [
            attributes
            for attributes in self.tagged("input")
            if attributes.get("name") == "csrfmiddlewaretoken"
        ]
        assert len(tokens) == len(forms)


def _search_page(graph: RbacGraph) -> bytes:
    client, receptionist = receptionist_client(graph)
    seed_patients(graph, receptionist.pk, graph.clinic_a, SEEDED)
    with runtime_role():
        response = client.post(
            patient_list_url(graph.clinic_a),
            {"q": "Marina", "page": "1"},
        )
    assert response.status_code == 200
    return response.content


def test_blank_search_screen_meets_the_dom_accessibility_contract(
    rbac_graph: RbacGraph,
) -> None:
    client, _ = receptionist_client(rbac_graph)

    with runtime_role():
        response = client.get(patient_list_url(rbac_graph.clinic_a))

    document = _Document(response.content)
    assert response.status_code == 200
    assert len(document.tagged("h1")) == 1
    document.assert_unique_identifiers()
    document.assert_descriptions_resolve()
    document.assert_every_control_is_labelled()
    document.assert_every_form_is_post_with_csrf()
    assert document.attributes_for("id_q")["aria-describedby"] == "patient-search-help"
    assert b'<a class="skip-link" href="#main-content">' in response.content


def test_search_results_expose_an_accessible_table_and_pagination(
    rbac_graph: RbacGraph,
) -> None:
    content = _search_page(rbac_graph)
    document = _Document(content)

    document.assert_unique_identifiers()
    document.assert_descriptions_resolve()
    document.assert_every_form_is_post_with_csrf()
    assert b"<caption>Registry matches in this clinic</caption>" in content
    assert document.attributes_for("patient-results-status")["role"] == "status"
    headers = document.tagged("th")
    assert {attributes.get("scope") for attributes in headers} == {"col", "row"}
    navigation = document.tagged("nav")
    assert [item.get("aria-label") for item in navigation] == ["Search result pages"]


def test_bound_search_errors_are_described_and_announced(
    rbac_graph: RbacGraph,
) -> None:
    client, _ = receptionist_client(rbac_graph)

    with runtime_role():
        response = client.post(
            patient_list_url(rbac_graph.clinic_a),
            {"q": "x", "page": "1"},
        )

    document = _Document(response.content)
    assert response.status_code == 200
    assert document.attributes_for("id_q")["aria-describedby"] == (
        "patient-search-help id_q_error"
    )
    assert document.attributes_for("intake-errors")["role"] == "alert"
    document.assert_unique_identifiers()
    document.assert_descriptions_resolve()
    document.assert_every_control_is_labelled()


def test_create_screen_meets_the_dom_accessibility_contract(
    rbac_graph: RbacGraph,
) -> None:
    client, _ = receptionist_client(rbac_graph)

    with runtime_role():
        response = client.get(patient_create_url(rbac_graph.clinic_a))

    document = _Document(response.content)
    assert response.status_code == 200
    assert len(document.tagged("h1")) == 1
    document.assert_unique_identifiers()
    document.assert_descriptions_resolve()
    document.assert_every_control_is_labelled()
    document.assert_every_form_is_post_with_csrf()
    assert document.attributes_for("id_full_name")["aria-describedby"] == (
        "patient-create-name-help"
    )
    assert document.attributes_for("id_birth_date")["aria-describedby"] == (
        "patient-create-birth-date-help"
    )


def test_bound_create_errors_are_described_and_announced(
    rbac_graph: RbacGraph,
) -> None:
    client, _ = receptionist_client(rbac_graph)

    with runtime_role():
        response = client.post(
            patient_create_url(rbac_graph.clinic_a),
            {"full_name": "", "birth_date": "", "idempotency_key": ""},
        )

    document = _Document(response.content)
    assert response.status_code == 200
    assert document.attributes_for("id_full_name")["aria-describedby"] == (
        "patient-create-name-help id_full_name_error"
    )
    assert document.attributes_for("intake-errors")["role"] == "alert"
    document.assert_unique_identifiers()
    document.assert_descriptions_resolve()


def test_intake_stylesheet_is_self_hosted_and_discoverable() -> None:
    assert finders.find("css/clinic-os-intake.css") is not None
