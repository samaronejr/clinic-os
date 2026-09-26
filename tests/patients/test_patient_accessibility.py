from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING, Final

import pytest
from apps.intake.models import Patient, PatientClinicEnrollment
from apps.tenancy.db import tenant_context
from django.contrib.staticfiles import finders
from django.middleware.csrf import (
    CSRF_ALLOWED_CHARS,
    CSRF_SECRET_LENGTH,
    CSRF_TOKEN_LENGTH,
)
from django.utils.formats import date_format
from django.utils.translation import gettext

from accessible_document import Document
from otp_test_support import runtime_role
from patient_http_support import (
    patient_create_url,
    patient_list_url,
    receptionist_client,
    seed_patients,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

SEEDED: Final = tuple(f"Marina Synthetic P{index:03d}" for index in range(30))
# Mirror of patient_http_support.seed_patients birth dates (days 1-27 of
# January 1990): asserted against the rows actually read back so the leak
# checks can never pass on an empty or drifted fixture set.
EXPECTED_SEEDED_BIRTH_DATES: Final = frozenset(
    date(1990, 1, day) for day in range(1, 28)
)
CSRF_TOKEN_PATTERN: Final = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')
ENROLLMENT_INPUT_PATTERN: Final = re.compile(r'name="enrollment_id" value="([^"]+)"')


def _unmask_csrf_token(token: str) -> str:
    """Mirror django.middleware.csrf._unmask_cipher_token (not stubbed).

    django-stubs exposes the length/charset constants but not the unmask
    helper, so the identical algorithm is repeated here: the first half is
    a mask applied character-wise over CSRF_ALLOWED_CHARS.
    """
    mask, cipher = token[:CSRF_SECRET_LENGTH], token[CSRF_SECRET_LENGTH:]
    pairs = zip(
        (CSRF_ALLOWED_CHARS.index(x) for x in cipher),
        (CSRF_ALLOWED_CHARS.index(x) for x in mask),
        strict=True,
    )
    return "".join(CSRF_ALLOWED_CHARS[x - y] for x, y in pairs)


def _without_known_identifiers(text: str, identifiers: Iterable[str]) -> str:
    """Blank only the exact identifier values this page legitimately renders.

    Exemption is by literal value, never by shape: a birth year spliced into
    a UUID-shaped string must still be caught, so pattern scrubbing is
    forbidden (hosted CI run 36091656875 showed real UUIDs can contain the
    year).
    """
    scrubbed = text
    for identifier in identifiers:
        scrubbed = scrubbed.replace(identifier, "")
    return scrubbed


def _search_page(graph: RbacGraph) -> tuple[bytes, str]:
    """Post the seeded search and return the body plus the CSRF cookie secret."""
    client, receptionist = receptionist_client(graph)
    seed_patients(graph, receptionist.pk, graph.clinic_a, SEEDED)
    with runtime_role():
        response = client.post(
            patient_list_url(graph.clinic_a),
            {"q": "Marina", "page": "1"},
        )
    assert response.status_code == 200
    csrf_cookie = client.cookies.get("csrftoken")
    assert csrf_cookie is not None
    csrf_secret = csrf_cookie.value
    if len(csrf_secret) == CSRF_TOKEN_LENGTH:
        # Django <4.0 masked the secret before storing it in the cookie.
        csrf_secret = _unmask_csrf_token(csrf_secret)
    assert len(csrf_secret) == CSRF_SECRET_LENGTH
    return response.content, csrf_secret


def test_blank_search_screen_meets_the_dom_accessibility_contract(
    rbac_graph: RbacGraph,
) -> None:
    client, _ = receptionist_client(rbac_graph)

    with runtime_role():
        response = client.get(patient_list_url(rbac_graph.clinic_a))

    document = Document(response.content)
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
    content, csrf_secret = _search_page(rbac_graph)
    document = Document(content)

    document.assert_unique_identifiers()
    document.assert_descriptions_resolve()
    document.assert_every_form_is_post_with_csrf()
    caption = gettext("Registry matches in this clinic")
    assert (
        f'<caption id="patient-results-caption">{caption}</caption>'.encode() in content
    )
    region = [
        attributes
        for attributes in document.tagged("div")
        if attributes.get("role") == "region"
    ]
    assert [item.get("aria-labelledby") for item in region] == [
        "patient-results-caption"
    ]
    assert region[0].get("tabindex") == "0"
    status = document.attributes_for("patient-results-status")
    assert status["role"] == "status"
    assert status["tabindex"] == "-1"
    assert "autofocus" in status
    headers = document.tagged("th")
    assert {attributes.get("scope") for attributes in headers} == {"col", "row"}
    assert {attributes.get("role") for attributes in headers} == {
        "columnheader",
        "rowheader",
    }
    # Birth dates are table cells only: never status text, labels or
    # attributes. Check the seeded patients' birth year plus every rendered
    # date format. The year check sees text with only the page's known
    # opaque identifiers removed; the rendered-date checks see raw text.
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        # Every seeded patient records a birth date; an unrecorded one
        # would be None and is not part of this page's fixture.
        birth_dates = [
            birth_date
            for birth_date in Patient.objects.filter(
                organization_id=rbac_graph.organization_a
            ).values_list("birth_date", flat=True)
            if birth_date is not None
        ]
        enrollment_ids = [
            str(enrollment_id)
            for enrollment_id in PatientClinicEnrollment.objects.filter(
                clinic_id=rbac_graph.clinic_a
            ).values_list("id", flat=True)
        ]
    assert birth_dates
    assert set(birth_dates) >= EXPECTED_SEEDED_BIRTH_DATES
    expected_years = {item.year for item in EXPECTED_SEEDED_BIRTH_DATES}
    assert {item.year for item in birth_dates} == expected_years
    birth_years = {str(year) for year in expected_years}
    rendered_dates = {
        rendered
        for birth_date in birth_dates
        for rendered in (
            birth_date.isoformat(),
            date_format(birth_date, "DATE_FORMAT"),
            date_format(birth_date, "SHORT_DATE_FORMAT"),
        )
    }
    # Exempt only literal opaque values known to this page: the rendered
    # enrollment UUIDs (provenance-checked against the seeded rows, since
    # page 1 shows only a page-size subset), the clinic UUID in URLs, and
    # this response's CSRF tokens. Patient UUIDs are never rendered, so
    # they are not exempted: every exempted value must actually occur,
    # which also keeps the exemption list from silently growing.
    text = content.decode()
    # A csrfmiddlewaretoken value is trusted only with independent
    # provenance: it must be a well-formed masked token that unmasks to
    # this client's csrftoken cookie secret. Anything else rendered in a
    # csrf input stays page text, so a leaked birth date there still fails.
    trusted_csrf = {
        candidate
        for candidate in CSRF_TOKEN_PATTERN.findall(text)
        if len(candidate) == CSRF_TOKEN_LENGTH
        and set(candidate) <= set(CSRF_ALLOWED_CHARS)
        and _unmask_csrf_token(candidate) == csrf_secret
    }
    assert trusted_csrf
    rendered_enrollments = set(ENROLLMENT_INPUT_PATTERN.findall(text))
    assert len(enrollment_ids) == len(SEEDED)
    assert rendered_enrollments
    assert rendered_enrollments <= set(enrollment_ids)
    known_identifiers = {
        str(rbac_graph.clinic_a),
        *rendered_enrollments,
        *trusted_csrf,
    }
    assert known_identifiers
    assert not [
        identifier for identifier in known_identifiers if identifier not in text
    ]
    # An exempted value must be an opaque identifier, never a birth date:
    # otherwise a leaked date could blanket-mask itself across the page.
    assert not [
        identifier
        for identifier in known_identifiers
        if any(rendered in identifier for rendered in rendered_dates)
    ]
    header = _without_known_identifiers(text.split("<tbody")[0], known_identifiers)
    raw_header = text.split("<tbody")[0]
    assert not [year for year in birth_years if year in header]
    assert not [rendered for rendered in rendered_dates if rendered in raw_header]
    assert not [
        attributes
        for _tag, attributes in document.elements
        if any(
            year in _without_known_identifiers(value or "", known_identifiers)
            for year in birth_years
            for value in attributes.values()
        )
    ]
    assert not [
        attributes
        for _tag, attributes in document.elements
        if any(
            rendered in (value or "")
            for rendered in rendered_dates
            for value in attributes.values()
        )
    ]
    pagination = [
        item
        for item in document.tagged("nav")
        if "intake-pagination" in (item.get("class") or "").split()
    ]
    assert [item.get("aria-label") for item in pagination] == [
        gettext("Search result pages")
    ]


def test_bound_search_errors_are_described_and_announced(
    rbac_graph: RbacGraph,
) -> None:
    client, _ = receptionist_client(rbac_graph)

    with runtime_role():
        response = client.post(
            patient_list_url(rbac_graph.clinic_a),
            {"q": "x", "page": "1"},
        )

    document = Document(response.content)
    assert response.status_code == 200
    assert document.attributes_for("id_q")["aria-describedby"] == (
        "patient-search-help id_q_error"
    )
    assert document.attributes_for("intake-errors")["role"] == "alert"
    document.assert_unique_identifiers()
    document.assert_descriptions_resolve()
    document.assert_every_control_is_labelled()


def test_empty_search_names_the_term_and_offers_registration(
    rbac_graph: RbacGraph,
) -> None:
    client, _ = receptionist_client(rbac_graph)

    with runtime_role():
        response = client.post(
            patient_list_url(rbac_graph.clinic_a),
            {"q": "Zeferino <Sintético>", "page": "1"},
        )

    document = Document(response.content)
    content = response.content.decode()
    assert response.status_code == 200
    status = document.attributes_for("patient-results-status")
    assert status["role"] == "status"
    assert "autofocus" in status
    assert (
        gettext("No patient named \u201c%(term)s\u201d in this clinic.")
        % {"term": "Zeferino &lt;Sintético&gt;"}
        in content
    )
    assert "<Sintético>" not in content
    assert (
        gettext("Check the spelling, or register the patient to book an appointment.")
        in content
    )
    assert f'href="{patient_create_url(rbac_graph.clinic_a)}"' in content
    assert "<table" not in content
    document.assert_unique_identifiers()
    document.assert_descriptions_resolve()


def test_registration_success_is_announced_once_on_the_search_screen(
    rbac_graph: RbacGraph,
) -> None:
    client, _ = receptionist_client(rbac_graph)
    payload = {
        "full_name": "Helena Synthetic Feedback",
        "birth_date": "1990-05-17",
        "idempotency_key": "6f3e0a4a-2f47-4c7e-9a9b-2c8e3d1f5b10",
    }

    with runtime_role():
        created = client.post(patient_create_url(rbac_graph.clinic_a), payload)
        landing = client.get(created.headers["Location"])
        reloaded = client.get(created.headers["Location"])

    assert created.status_code == 303
    document = Document(landing.content)
    notice = document.attributes_for("intake-registered")
    assert notice["role"] == "status"
    assert gettext("Patient registered") in landing.content.decode()
    # Completion feedback carries no patient data and is consumed once.
    assert b"Helena Synthetic Feedback" not in landing.content
    assert b"1990" not in landing.content
    assert b"intake-registered" not in reloaded.content


def test_create_screen_meets_the_dom_accessibility_contract(
    rbac_graph: RbacGraph,
) -> None:
    client, _ = receptionist_client(rbac_graph)

    with runtime_role():
        response = client.get(patient_create_url(rbac_graph.clinic_a))

    document = Document(response.content)
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

    document = Document(response.content)
    assert response.status_code == 200
    assert document.attributes_for("id_full_name")["aria-describedby"] == (
        "patient-create-name-help id_full_name_error"
    )
    errors = document.attributes_for("intake-errors")
    assert errors["role"] == "alert"
    assert errors["tabindex"] == "-1"
    assert "autofocus" in errors
    document.assert_unique_identifiers()
    document.assert_descriptions_resolve()


def test_invalid_create_preserves_the_typed_name_for_retry(
    rbac_graph: RbacGraph,
) -> None:
    client, _ = receptionist_client(rbac_graph)

    with runtime_role():
        response = client.post(
            patient_create_url(rbac_graph.clinic_a),
            {
                "full_name": "Teste Synthetic Retry",
                "birth_date": "3999-01-01",
                "idempotency_key": "6f3e0a4a-2f47-4c7e-9a9b-2c8e3d1f5b11",
            },
        )

    document = Document(response.content)
    assert response.status_code == 200
    assert document.attributes_for("id_full_name")["value"] == "Teste Synthetic Retry"
    assert document.attributes_for("id_birth_date")["value"] == "3999-01-01"
    assert document.attributes_for("intake-errors")["role"] == "alert"
    assert (
        gettext(
            "Check the registration fields: the birth date cannot be in the future "
            "and the document number must be valid."
        )
        in response.content.decode()
    )


def test_intake_stylesheet_is_self_hosted_and_discoverable() -> None:
    assert finders.find("css/clinic-os-intake.css") is not None
