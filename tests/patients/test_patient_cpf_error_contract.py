from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.core.patient_context import PATIENT_BOUND_VIEWS
from apps.core.workspace_access import _billing_surface_admitted
from apps.identity.models import User, UserClinicRole
from apps.intake.demographics import (
    DemographicsInputError,
    DemographicsNameRequiredError,
    add_identifier,
    update_demographics,
)
from apps.intake.models import Patient, PatientClinicEnrollment
from apps.intake.services import register_patient
from apps.tenancy.db import tenant_context
from django.contrib.auth.hashers import make_password
from django.db import connection, transaction
from django.test import RequestFactory
from django.urls import resolve, reverse
from django.utils.translation import gettext
from psycopg import sql

from core.test_navigation import _client_for, _get, _post
from database_urls import database_url_for_name
from identity.permission_support import permission_context
from otp_test_support import runtime_role
from patient_http_support import (
    audit_event_types,
    patient_create_url,
    receptionist_client,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


_INVALID_CPF = "11111111111"
_WRONG_CHECK_DIGIT_CPF = "52998224724"
_VALID_CPF = "52998224725"


def _create_payload(**overrides: str) -> dict[str, str]:
    payload = {
        "full_name": "Celia Synthetic Testpatient",
        "birth_date": "1988-04-05",
        "idempotency_key": str(uuid4()),
    }
    payload.update(overrides)
    return payload


def test_create_post_repeated_digit_cpf_returns_a_handled_error(
    rbac_graph: RbacGraph,
) -> None:
    client, _ = receptionist_client(rbac_graph)

    with runtime_role():
        response = client.post(
            patient_create_url(rbac_graph.clinic_a),
            _create_payload(
                identifier_kind="cpf",
                identifier_value=_INVALID_CPF,
            ),
        )

    assert response.status_code == 200
    assert response.headers.get("Location") is None
    errors = response.context["form"].errors
    assert errors["identifier_value"] == [gettext("Enter a valid document number.")]
    assert b"intake-errors" in response.content


def test_create_post_wrong_check_digit_cpf_returns_a_handled_error(
    rbac_graph: RbacGraph,
) -> None:
    client, _ = receptionist_client(rbac_graph)

    with runtime_role():
        response = client.post(
            patient_create_url(rbac_graph.clinic_a),
            _create_payload(
                identifier_kind="cpf",
                identifier_value=_WRONG_CHECK_DIGIT_CPF,
            ),
        )

    assert response.status_code == 200
    errors = response.context["form"].errors
    assert errors["identifier_value"] == [gettext("Enter a valid document number.")]
    assert b"intake-errors" in response.content


def test_create_post_invalid_cpf_htmx_returns_the_error_panel(
    rbac_graph: RbacGraph,
) -> None:
    client, _ = receptionist_client(rbac_graph)

    with runtime_role():
        response = client.post(
            patient_create_url(rbac_graph.clinic_a),
            _create_payload(
                identifier_kind="cpf",
                identifier_value=_INVALID_CPF,
            ),
            headers={"hx-request": "true"},
        )

    assert response.status_code == 200
    assert response.headers.get("HX-Redirect") is None
    errors = response.context["form"].errors
    assert errors["identifier_value"] == [gettext("Enter a valid document number.")]
    assert b"intake-errors" in response.content


def test_create_post_unprocessable_name_returns_a_handled_error(
    rbac_graph: RbacGraph,
) -> None:
    client, _ = receptionist_client(rbac_graph)

    with runtime_role():
        response = client.post(
            patient_create_url(rbac_graph.clinic_a),
            _create_payload(social_name="Social\x01Name"),
        )

    assert response.status_code == 200
    errors = response.context["form"].errors
    assert errors["social_name"] == [gettext("Check the entered values.")]
    assert b"intake-errors" in response.content


def test_create_post_valid_cpf_still_registers(rbac_graph: RbacGraph) -> None:
    client, _ = receptionist_client(rbac_graph)

    with runtime_role():
        response = client.post(
            patient_create_url(rbac_graph.clinic_a),
            _create_payload(
                identifier_kind="cpf",
                identifier_value=_VALID_CPF,
            ),
        )

    assert response.status_code == 303
    assert response.headers["Location"].endswith("/patients/")
    assert _VALID_CPF not in response.headers["Location"]


# --- Todo 17 R5 repair: edit-path handled-error contract ---
#
# The demographics edit view must surface the same handled field errors the
# create path already returns. A service rejection can never escape as an
# HTTP 500, and a domain error whose origin is not the identifier must never
# be guessed onto the identifier field.


def _registered(
    rbac_graph: RbacGraph,
) -> tuple[Patient, int]:
    """Register one patient through the service and return (patient, version)."""
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        outcome = register_patient(
            clinic_id=rbac_graph.clinic_a,
            idempotency_key=uuid4(),
            legal_name="Celia R5 Editpatient",
            social_name="",
            birth_date=None,
        )
    return outcome.registration.patient, 1


def _demographics_url(rbac_graph: RbacGraph) -> str:
    return reverse("intake:patient-demographics", args=(rbac_graph.clinic_a,))


def _manage_enrollment(
    rbac_graph: RbacGraph, patient: Patient
) -> PatientClinicEnrollment:
    """Return the enrollment the registered patient holds in clinic_a."""
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        return PatientClinicEnrollment.objects.get(
            clinic_id=rbac_graph.clinic_a, patient=patient
        )


def test_edit_post_invalid_cpf_returns_a_handled_error(
    rbac_graph: RbacGraph,
) -> None:
    patient, _version = _registered(rbac_graph)
    enrollment = _manage_enrollment(rbac_graph, patient)
    client, receptionist = receptionist_client(rbac_graph)
    events_before = audit_event_types(rbac_graph, receptionist.pk)

    with runtime_role():
        response = client.post(
            _demographics_url(rbac_graph),
            {
                "enrollment_id": str(enrollment.pk),
                "action": "add_identifier",
                "kind": "cpf",
                "value": _INVALID_CPF,
            },
        )

    assert response.status_code == 200
    errors = response.context["rejected_form"].errors
    assert errors["value"] == [gettext("Enter a valid document number.")]
    assert audit_event_types(rbac_graph, receptionist.pk) == events_before

    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
        pytest.raises(DemographicsInputError),
    ):
        add_identifier(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=enrollment.pk,
            kind="cpf",
            value=_INVALID_CPF,
        )


def test_edit_post_both_names_empty_returns_a_handled_error(
    rbac_graph: RbacGraph,
) -> None:
    patient, version = _registered(rbac_graph)
    enrollment = _manage_enrollment(rbac_graph, patient)
    client, receptionist = receptionist_client(rbac_graph)
    events_before = audit_event_types(rbac_graph, receptionist.pk)

    with runtime_role():
        response = client.post(
            _demographics_url(rbac_graph),
            {
                "enrollment_id": str(enrollment.pk),
                "action": "save",
                "expected_version": str(version),
                "legal_name": "",
                "social_name": "",
            },
        )

    assert response.status_code == 200
    errors = response.context["rejected_form"].errors
    assert errors["legal_name"] == [gettext("Enter a legal or a social name.")]
    assert audit_event_types(rbac_graph, receptionist.pk) == events_before

    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
        pytest.raises(DemographicsNameRequiredError),
    ):
        update_demographics(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=enrollment.pk,
            expected_version=version,
            changes={"legal_name": "", "social_name": ""},
            reason="",
        )


def test_edit_post_overlong_social_name_never_blames_the_identifier(
    rbac_graph: RbacGraph,
) -> None:
    patient, version = _registered(rbac_graph)
    enrollment = _manage_enrollment(rbac_graph, patient)
    client, _ = receptionist_client(rbac_graph)

    with runtime_role():
        response = client.post(
            _demographics_url(rbac_graph),
            {
                "enrollment_id": str(enrollment.pk),
                "action": "save",
                "expected_version": str(version),
                "legal_name": "",
                "social_name": "x" * 300,
            },
        )

    assert response.status_code == 200
    errors = response.context["rejected_form"].errors
    assert "identifier_value" not in errors
    assert "social_name" in errors


def test_edit_post_retire_missing_identifier_returns_a_handled_error(
    rbac_graph: RbacGraph,
) -> None:
    patient, _version = _registered(rbac_graph)
    enrollment = _manage_enrollment(rbac_graph, patient)
    client, _ = receptionist_client(rbac_graph)

    with runtime_role():
        response = client.post(
            _demographics_url(rbac_graph),
            {
                "enrollment_id": str(enrollment.pk),
                "action": "retire_identifier",
                "expected_version": "1",
                "kind": "cpf",
            },
        )

    assert response.status_code == 200
    errors = response.context["rejected_form"].errors
    assert errors["__all__"] == [gettext("Check the entered values.")]


def test_patient_bound_view_registry_covers_demographics() -> None:
    assert "intake:patient-demographics" in PATIENT_BOUND_VIEWS


def test_patient_reported_record_renders_the_unverified_marker(
    rbac_graph: RbacGraph,
) -> None:
    patient, version = _registered(rbac_graph)
    enrollment = _manage_enrollment(rbac_graph, patient)
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        update_demographics(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=enrollment.pk,
            expected_version=version,
            changes={"preferred_name": "Nina R5"},
            reason="Patient updated their own record",
            source="patient_reported",
        )
    client, _ = receptionist_client(rbac_graph)

    with runtime_role():
        response = client.post(
            _demographics_url(rbac_graph),
            {
                "enrollment_id": str(enrollment.pk),
                "action": "manage",
            },
        )

    assert response.status_code == 200
    assert response.context["profile"].source == "patient_reported"
    marker = gettext("Reported by the patient; not verified by staff.")
    assert marker in response.content.decode()


def test_demographics_view_binds_the_selected_patient_banner(
    rbac_graph: RbacGraph,
) -> None:
    patient_a, _ = _registered(rbac_graph)
    patient_b, _ = _registered(rbac_graph)
    enrollment_a = _manage_enrollment(rbac_graph, patient_a)
    enrollment_b = _manage_enrollment(rbac_graph, patient_b)
    client, _ = receptionist_client(rbac_graph)
    url = _demographics_url(rbac_graph)

    with runtime_role():
        first = client.post(
            url,
            {
                "enrollment_id": str(enrollment_a.pk),
                "action": "manage",
            },
        )
        second = client.post(
            url,
            {
                "enrollment_id": str(enrollment_b.pk),
                "action": "manage",
            },
        )

    assert first.status_code == 200
    assert second.status_code == 200
    workspace = second.context["workspace"]
    assert workspace.patient is not None
    assert workspace.patient.enrollment_id == enrollment_b.pk


def _clinic_role_user(
    graph: RbacGraph, role: UserClinicRole.Role, clinic_id: UUID
) -> UUID:
    """Create one user holding ``role`` on the given clinic."""
    user = User.objects.create(
        username=f"r5-{role}-{uuid4().hex}",
        password=make_password("todo17-role-credential"),
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(graph.organization_a)],
        )
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=clinic_id,
            user_id=user.pk,
            role=role,
        )
    return user.pk


def test_demographics_post_unknown_action_is_refused(rbac_graph: RbacGraph) -> None:
    """An action the profile dispatcher never routes reaches its 404 refusal."""
    patient, _version = _registered(rbac_graph)
    enrollment = _manage_enrollment(rbac_graph, patient)
    client, _receptionist = receptionist_client(rbac_graph)
    with runtime_role():
        response = client.post(
            _demographics_url(rbac_graph),
            {
                "action": "bogus-action",
                "enrollment_id": str(enrollment.pk),
            },
        )
    assert response.status_code == 404


def test_demographics_manage_cross_clinic_enrollment_is_refused(
    rbac_graph: RbacGraph,
) -> None:
    """A same-org foreign-clinic enrollment fails both read surfaces."""
    clinic_b_receptionist = _clinic_role_user(
        rbac_graph, UserClinicRole.Role.RECEPTIONIST, rbac_graph.clinic_b
    )
    with (
        runtime_role(),
        tenant_context(clinic_b_receptionist, rbac_graph.organization_a),
    ):
        outcome = register_patient(
            clinic_id=rbac_graph.clinic_b,
            legal_name="Nome Clinica B Sintetico",
            social_name="",
            birth_date=None,
            idempotency_key=uuid4(),
        )
    enrollment_b = outcome.registration.enrollment.pk
    client, _receptionist = receptionist_client(rbac_graph)
    with runtime_role():
        response = client.post(
            _demographics_url(rbac_graph),
            {"action": "manage", "enrollment_id": str(enrollment_b)},
        )
    assert response.status_code == 404


def test_finance_actor_is_refused_on_other_patient_bound_routes(
    rbac_graph: RbacGraph,
) -> None:
    """The billing-read gate admission stays scoped to the demographics surface.

    The same finance actor that reaches the minimal billing identity page is
    still refused before every other patient-bound view.
    """
    patient, _version = _registered(rbac_graph)
    enrollment = _manage_enrollment(rbac_graph, patient)
    finance = _clinic_role_user(
        rbac_graph, UserClinicRole.Role.FINANCE, rbac_graph.clinic_a
    )
    finance_user = User.objects.get(pk=finance)
    credential = str(uuid4())
    finance_user.set_password(credential)
    finance_user.save(update_fields=["password"])
    client, _receptionist = receptionist_client(rbac_graph)
    with runtime_role():
        assert client.login(username=finance_user.username, password=credential)
        response = client.post(
            reverse("intake:patient-contacts", args=(rbac_graph.clinic_a,)),
            {
                "action": "manage",
                "enrollment_id": str(enrollment.pk),
            },
        )
    assert response.status_code == 404


def test_demographics_save_by_read_only_role_is_refused(
    rbac_graph: RbacGraph,
) -> None:
    """A nurse passes the page gate but the write guard refuses the save."""
    patient, _version = _registered(rbac_graph)
    enrollment = _manage_enrollment(rbac_graph, patient)
    nurse = _clinic_role_user(
        rbac_graph, UserClinicRole.Role.NURSE, rbac_graph.clinic_a
    )
    nurse_user = User.objects.get(pk=nurse)
    credential = str(uuid4())
    nurse_user.set_password(credential)
    nurse_user.save(update_fields=["password"])
    client, _receptionist = receptionist_client(rbac_graph)
    with runtime_role():
        assert client.login(username=nurse_user.username, password=credential)
        response = client.post(
            _demographics_url(rbac_graph),
            {
                "action": "save",
                "enrollment_id": str(enrollment.pk),
                "expected_version": "1",
            },
        )
    assert response.status_code == 404


def _physical_rows() -> dict[str, list[tuple[str, str, str]]]:
    """Observe physical rows independently of runtime RLS, without row values."""
    url = database_url_for_name(
        os.environ["TEST_SUPERUSER_DATABASE_URL"],
        str(connection.settings_dict["NAME"]),
    )
    with psycopg.connect(url) as observer:
        tables = observer.execute(
            "SELECT relname FROM pg_class JOIN pg_namespace "
            "ON pg_namespace.oid = relnamespace WHERE nspname = 'clinic_app' "
            "AND relkind = 'r' AND relname <> 'django_session' ORDER BY relname"
        ).fetchall()
        return {
            name: observer.execute(
                sql.SQL(
                    "SELECT ctid::text, xmin::text, md5(row_to_json(t)::text) "
                    "FROM clinic_app.{} AS t ORDER BY ctid"
                ).format(sql.Identifier(name))
            ).fetchall()
            for (name,) in tables
        }


def _four_gucs() -> tuple[str | None, ...]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_user, current_setting('app.current_tenant', true), "
            "current_setting('app.current_user_id', true), "
            "current_setting('app.current_patient_session', true), "
            "current_setting('app.current_actor_service', true)"
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "clinic_app"
        return tuple(row[1:])


def _refused_request(
    client: Client, url: str, data: dict[str, str]
) -> _MonkeyPatchedWSGIResponse:
    before = _physical_rows()
    session_before = dict(client.session)
    with runtime_role():
        gucs_before = _four_gucs()
        response = _post(client, url, data) if data else _get(client, url)
        gucs_after = _four_gucs()
    assert response.status_code in {403, 404}
    assert b"data-patient-banner" not in response.content
    assert _physical_rows() == before
    assert dict(client.session) == session_before
    assert gucs_after == gucs_before
    return response


@pytest.mark.parametrize("htmx", [False, True])
@pytest.mark.parametrize("action", ["save", "add_identifier"])
def test_rejected_form_valid_edit_conserves_physical_state(
    rbac_graph: RbacGraph,
    action: str,
    htmx: bool,
    record_property: Callable[[str, object], None],
) -> None:
    patient, version = _registered(rbac_graph)
    enrollment = _manage_enrollment(rbac_graph, patient)
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        if action == "save":
            update_demographics(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=enrollment.pk,
                expected_version=version,
                changes={"preferred_name": "Synthetic Current"},
                reason="Synthetic correction",
            )
        else:
            add_identifier(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=enrollment.pk,
                kind="cpf",
                value=_VALID_CPF,
            )
    client, _ = receptionist_client(rbac_graph)
    url = _demographics_url(rbac_graph)
    with runtime_role():
        assert (
            client.post(
                url, {"enrollment_id": str(enrollment.pk), "action": "manage"}
            ).status_code
            == 200
        )
    before = _physical_rows()
    session_before = dict(client.session)
    payload = {
        "enrollment_id": str(enrollment.pk),
        "action": action,
        "expected_version": str(version),
        "legal_name": "Synthetic Stale",
        "reason": "Synthetic rejected correction",
        "kind": "cpf",
        "value": _VALID_CPF,
    }
    with runtime_role():
        gucs_before = _four_gucs()
        response = client.post(
            url, payload, headers={"hx-request": "true"} if htmx else {}
        )
        gucs_after = _four_gucs()
    after = _physical_rows()
    record_property(
        "rejection_observation",
        json.dumps(
            {
                "action": action,
                "htmx": htmx,
                "status": response.status_code,
                "changedPhysicalTables": [
                    name for name in before if before[name] != after[name]
                ],
                "fourGucsBefore": gucs_before,
                "fourGucsAfter": gucs_after,
                "sessionConserved": dict(client.session) == session_before,
                "physicalBefore": before,
                "physicalAfter": after,
            },
            sort_keys=True,
        ),
    )
    assert response.status_code == 200
    assert gucs_after == gucs_before
    assert dict(client.session) == session_before
    assert after == before


@pytest.mark.parametrize("field", ["social_name", "full_name"])
@pytest.mark.parametrize("htmx", [False, True])
def test_valid_cpf_registration_attributes_demographic_error_to_origin(
    rbac_graph: RbacGraph,
    field: str,
    htmx: bool,
    record_property: Callable[[str, object], None],
) -> None:
    client, _ = receptionist_client(rbac_graph)
    with runtime_role():
        assert client.get(patient_create_url(rbac_graph.clinic_a)).status_code == 200
    before = _physical_rows()
    session_before = dict(client.session)
    with runtime_role():
        gucs_before = _four_gucs()
        response = client.post(
            patient_create_url(rbac_graph.clinic_a),
            _create_payload(
                identifier_kind="cpf",
                identifier_value=_VALID_CPF,
                **{field: "Synthetic\x01Invalid"},
            ),
            headers={"hx-request": "true"} if htmx else {},
        )
        gucs_after = _four_gucs()
    errors = response.context["form"].errors
    after = _physical_rows()
    record_property(
        "registration_observation",
        json.dumps(
            {
                "origin": field,
                "htmx": htmx,
                "status": response.status_code,
                "errorFields": sorted(errors),
                "physicalConserved": before == after,
                "sessionConserved": dict(client.session) == session_before,
                "fourGucsBefore": gucs_before,
                "fourGucsAfter": gucs_after,
            },
            sort_keys=True,
        ),
    )
    assert response.status_code == 200
    assert gucs_after == gucs_before
    assert dict(client.session) == session_before
    assert after == before
    assert set(errors) == {field}


@pytest.mark.parametrize(
    "action",
    [
        "save",
        "add_identifier",
        "retire_identifier",
        "save_address",
        "retire_address",
        "save_contact",
        "retire_contact",
        "save_membership",
        "retire_membership",
    ],
)
def test_finance_minimal_read_never_admits_a_mutation(
    rbac_graph: RbacGraph, action: str
) -> None:
    patient, _ = _registered(rbac_graph)
    enrollment = _manage_enrollment(rbac_graph, patient)
    client, _ = _client_for(rbac_graph, "finance")
    url = _demographics_url(rbac_graph)
    base = {"enrollment_id": str(enrollment.pk), "action": "manage"}
    minimal = _post(client, url, base)
    assert minimal.status_code == 200
    assert "identity" in minimal.context
    assert "profile" not in minimal.context
    assert b"data-patient-banner" not in minimal.content
    assert b"<form" not in minimal.content
    assert b"demographic-fields-title" not in minimal.content
    assert b"identifiers-title" not in minimal.content
    response = _refused_request(
        client,
        url,
        base
        | {
            "action": action,
            "expected_version": "1",
            "kind": "cpf",
            "value": _VALID_CPF,
            "legal_name": "Synthetic Refused",
            "sequence": "1",
            "street": "Synthetic Street",
            "payer_name": "Synthetic Payer",
            "name": "Synthetic Contact",
        },
    )
    assert response.status_code == 404


@pytest.mark.parametrize("revocation", ["permission", "membership"])
def test_billing_admission_rechecks_authority_on_same_connection_and_next_request(
    rbac_graph: RbacGraph, revocation: str
) -> None:
    patient, _ = _registered(rbac_graph)
    enrollment = _manage_enrollment(rbac_graph, patient)
    client, actor = _client_for(rbac_graph, "finance")
    url = _demographics_url(rbac_graph)
    payload = {"enrollment_id": str(enrollment.pk), "action": "manage"}
    assert _post(client, url, payload).status_code == 200
    request = RequestFactory().post(url, payload)
    request.resolver_match = resolve(url)
    owner_url = database_url_for_name(
        os.environ["MIGRATION_DATABASE_URL"], str(connection.settings_dict["NAME"])
    )
    with permission_context(rbac_graph, actor.pk):
        assert _billing_surface_admitted(request, rbac_graph.clinic_a)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            backend_before = cursor.fetchone()
        # This independent owner's context exit is the actual commit boundary.
        with psycopg.connect(owner_url) as owner:
            owner.execute(
                "SELECT set_config('app.current_tenant', %s, true), "
                "set_config('app.current_user_id', %s, true)",
                [str(rbac_graph.organization_a), str(rbac_graph.shared_user)],
            )
            if revocation == "permission":
                owner.execute(
                    "INSERT INTO clinic_app.identity_rolegrant "
                    "(id, organization_id, clinic_id, role, permission, "
                    "bundle_version, effect, valid_from) "
                    "VALUES (%s, %s, %s, 'finance', 'demographics.billing_read', "
                    "1, 'remove', now())",
                    [uuid4(), rbac_graph.organization_a, rbac_graph.clinic_a],
                )
            else:
                owner.execute(
                    "UPDATE clinic_app.identity_userclinicrole SET clinic_id=%s "
                    "WHERE user_id=%s AND clinic_id=%s",
                    [rbac_graph.clinic_b, actor.pk, rbac_graph.clinic_a],
                )
        physical_before = _physical_rows()
        gucs_before = _four_gucs()
        assert not _billing_surface_admitted(request, rbac_graph.clinic_a)
        assert _physical_rows() == physical_before
        assert _four_gucs() == gucs_before
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            assert cursor.fetchone() == backend_before
    physical_before = _physical_rows()
    session_before = dict(client.session)
    refused = _post(client, url, payload)
    assert refused.status_code == 404
    assert _physical_rows() == physical_before
    assert dict(client.session) == session_before


def test_billing_admission_is_refused_for_a_different_resolved_route(
    rbac_graph: RbacGraph,
) -> None:
    _, actor = _client_for(rbac_graph, "finance")
    request = RequestFactory().post(
        reverse("intake:patient-contacts", args=(rbac_graph.clinic_a,))
    )
    request.resolver_match = resolve(request.path)
    before = _physical_rows()
    with permission_context(rbac_graph, actor.pk):
        gucs_before = _four_gucs()
        assert not _billing_surface_admitted(request, rbac_graph.clinic_a)
        assert _four_gucs() == gucs_before
    assert _physical_rows() == before
