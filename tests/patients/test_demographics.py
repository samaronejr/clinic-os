"""Todo 17: demographics versions, blind-indexed identifiers and history.

Covers the plan's acceptance rows: strict CPF check-digit rejection,
blind-index exact search with org isolation, append-only version history
with ``demographics_stale`` conflicts enforced in the database for every
identity table, permission-scoped access (finance gets only the billing
read), registration without invented values, deliberate 'unknown/declined'
answers, the clinic-required-field policy as clinic configuration, the
staff edit surface and patient-reported carry-forward.
"""

from __future__ import annotations

import contextlib
import importlib
import os
from datetime import date
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.identity.models import User, UserClinicRole
from apps.intake.models import (
    DemographicsCorrection,
    EmergencyContact,
    Patient,
    PatientAddress,
    PatientClinicEnrollment,
    PatientDemographics,
    PatientIdentifier,
)
from apps.intake.patient_access import patient_session_context, redeem_invitation
from apps.tenancy.db import tenant_context
from apps.tenancy.envelope import BlindIndex, blind_indexes
from django.contrib.auth.hashers import make_password
from django.db import IntegrityError, connection, transaction
from django.db.migrations import RunPython
from django.urls import reverse
from django.utils.translation import gettext
from psycopg.errors import CheckViolation, InsufficientPrivilege

from accessible_document import Document
from database_urls import database_url_for_name
from patient_http_support import receptionist_client
from patient_service_support import runtime_role
from tenant_probe_support import assert_no_cross_tenant_rows

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


_SEED_ERROR = "seed must be nine digits"


def _cpf_for(seed: str) -> str:
    """Return a mod-11-valid CPF for nine arbitrary digits."""
    if len(seed) != 9 or not seed.isdigit():
        raise AssertionError(_SEED_ERROR)
    first = sum(int(seed[i]) * (10 - i) for i in range(9)) % 11
    d1 = 0 if first < 2 else 11 - first
    second = (sum(int(seed[i]) * (11 - i) for i in range(9)) + d1 * 2) % 11
    d2 = 0 if second < 2 else 11 - second
    return f"{seed}{d1}{d2}"


_CPF_A = _cpf_for("529982247")
_CPF_B = _cpf_for("390533447")


def _services() -> ModuleType:
    return importlib.import_module("apps.intake.services")


def _register(services: ModuleType, graph: RbacGraph, **overrides: object) -> Any:  # noqa: ANN401 - service module is dynamic
    """Create one patient in clinic A with deterministic defaults."""
    kwargs = {
        "clinic_id": graph.clinic_a,
        "full_name": "Paciente Sintetico",
        "birth_date": date(1990, 5, 17),
        "idempotency_key": uuid4(),
    }
    kwargs.update(overrides)
    return services.create_patient(**kwargs)


def _digests_for(organization_id: UUID) -> tuple[BlindIndex, ...]:
    """Return blind indexes for ``_CPF_A`` under one tenant's DEK."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        return blind_indexes(
            purpose="intake.patientidentifier.value",
            plaintext=f"cpf:{_CPF_A}".encode(),
        )


def _set_local(connection_: psycopg.Connection, value: str) -> None:
    connection_.execute(
        "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
        (value,),
    )


def _org_b_patient(graph: RbacGraph) -> Patient:
    """Seed one org-B patient directly (org A has no org-B manager role)."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(graph.organization_b)],
        )
        patient = Patient.objects.create(
            organization_id=graph.organization_b,
            full_name="Org B Paciente",
            birth_date=date(1985, 3, 3),
        )
        PatientClinicEnrollment.objects.create(
            organization_id=graph.organization_b,
            clinic_id=graph.clinic_c,
            patient_id=patient.pk,
            idempotency_key=uuid4(),
            create_fingerprint=b"\x00" * 32,
        )
        return patient


APPEND_ONLY = "patient identity history is append-only"


def _role_user(graph: RbacGraph, role: UserClinicRole.Role) -> UUID:
    """Create one user holding ``role`` on clinic A (seeded as owner)."""
    user = User.objects.create(
        username=f"todo17-{role}-{uuid4().hex}",
        password=make_password("todo17-role-credential"),
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(graph.organization_a)],
        )
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=user.pk,
            role=role,
        )
    return user.pk


@contextlib.contextmanager
def _database(url_variable: str, organization_id: UUID) -> Iterator[psycopg.Cursor]:
    """Open one raw autocommit connection bound to the organization.

    Session scope (is_local=false) survives each autocommit statement.
    """
    url = database_url_for_name(
        os.environ[url_variable], str(connection.settings_dict["NAME"])
    )
    with psycopg.connect(url, autocommit=True) as raw, raw.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, false)",
            [str(organization_id)],
        )
        yield cursor


def test_cpf_check_digits_enforced_and_identifier_masked(
    rbac_graph: RbacGraph,
) -> None:
    services = _services()
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        registration = _register(services, rbac_graph)
        with pytest.raises(services.DemographicsInputError):
            services.add_identifier(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=registration.enrollment.pk,
                kind="cpf",
                value="111.111.111-11",
            )
        with pytest.raises(services.DemographicsInputError):
            services.add_identifier(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=registration.enrollment.pk,
                kind="cpf",
                value="123.456.789-00",
            )
        outcome = services.add_identifier(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
            kind="cpf",
            value="529.982.247-25",
        )
        assert outcome.matching_enrollment_id is None
        profile = services.demographics_profile(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
        )
        assert len(profile.identifiers) == 1
        assert profile.identifiers[0].kind == "cpf"
        assert profile.identifiers[0].masked_value.endswith("4725")
        assert _CPF_A not in profile.identifiers[0].masked_value
        stored = PatientIdentifier.objects.get(pk=outcome.identifier_id)
        assert stored.value == _CPF_A
        assert len(stored.blind_index) == 32


def test_identifier_blind_index_exact_search_and_cross_tenant_isolation(
    tenant_probe_pair: RbacGraph,
) -> None:
    services = _services()
    graph = tenant_probe_pair
    org_b_patient = _org_b_patient(graph)
    with (
        runtime_role(),
        tenant_context(graph.shared_user, graph.organization_a),
    ):
        registration = _register(services, graph)
        services.add_identifier(
            clinic_id=graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
            kind="cpf",
            value=_CPF_A,
        )
        page = services.search_patient_identifiers(
            clinic_id=graph.clinic_a, kind="cpf", value=_CPF_A
        )
        assert [item.enrollment_id for item in page.items] == [
            registration.enrollment.pk
        ]
        assert page.items[0].display_name == registration.patient.full_name
        # Formatted vs bare digits normalize to the same digest.
        formatted = services.search_patient_identifiers(
            clinic_id=graph.clinic_a,
            kind="cpf",
            value="529.982.247-25",
        )
        assert [item.enrollment_id for item in formatted.items] == [
            registration.enrollment.pk
        ]
        no_match = services.search_patient_identifiers(
            clinic_id=graph.clinic_a, kind="cpf", value=_CPF_B
        )
        assert no_match.items == ()

    # Seed an org-B identifier row for the same CPF through the same
    # wrapper: the org-B DEK must produce a different digest, so the same
    # document in another organization can never collide on the index.
    digests_a = _digests_for(graph.organization_a)
    digests_b = _digests_for(graph.organization_b)
    assert digests_a
    assert digests_b
    assert digests_a[-1].digest != digests_b[-1].digest
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(graph.organization_b)],
        )
        PatientIdentifier.objects.create(
            organization_id=graph.organization_b,
            patient_id=org_b_patient.pk,
            clinic_id=graph.clinic_c,
            kind="cpf",
            value=_CPF_A,
            blind_index=digests_b[-1].digest,
            index_key_version=digests_b[-1].key_version,
        )
    with (
        runtime_role(),
        tenant_context(graph.shared_user, graph.organization_b),
    ):
        org_b_page = services.search_patient_identifiers(
            clinic_id=graph.clinic_c, kind="cpf", value=_CPF_A
        )
        assert [item.enrollment_id for item in org_b_page.items] != []
        assert all(
            item.enrollment_id != registration.enrollment.pk
            for item in org_b_page.items
        )
    assert_no_cross_tenant_rows(graph, PatientIdentifier)


def test_demographics_versions_conflict_receipts_and_immutability(
    rbac_graph: RbacGraph,
) -> None:
    services = _services()
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        registration = _register(services, rbac_graph)
        first = services.update_demographics(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
            expected_version=0,
            changes={"legal_name": "Paciente Sintetico", "social_name": "Cina"},
            reason="registration intake",
        )
        assert first.version == 1
        registration.patient.refresh_from_db()
        assert registration.patient.full_name == "Paciente Sintetico"
        with pytest.raises(services.DemographicsStaleError) as conflict:
            services.update_demographics(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=registration.enrollment.pk,
                expected_version=0,
                changes={"social_name": "Outra"},
                reason="stale write",
            )
        assert str(conflict.value) == "demographics_stale"
        second = services.update_demographics(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
            expected_version=1,
            changes={"social_name": "Cina B"},
            reason="patient asked to correct",
        )
        assert second.version == 2
        receipts = list(
            DemographicsCorrection.objects.filter(
                organization_id=rbac_graph.organization_a,
                patient_id=registration.patient.pk,
            ).order_by("demographics__version")
        )
        assert [receipt.demographics.version for receipt in receipts] == [1, 2]
        assert receipts[0].previous is None
        assert receipts[1].previous_id == first.pk
        assert receipts[1].changed_fields == ["social_name"]
        assert receipts[1].reason == "patient asked to correct"
        # clinic_app holds no UPDATE/DELETE grant on the append-only
        # tables; prove it on a raw runtime-role connection.
        app_url = database_url_for_name(
            os.environ["APP_DATABASE_URL"],
            str(connection.settings_dict["NAME"]),
        )
        with psycopg.connect(app_url) as app_connection:
            _set_local(app_connection, str(rbac_graph.organization_a))
            with pytest.raises(InsufficientPrivilege):
                app_connection.execute(
                    "UPDATE clinic_app.intake_patientdemographics "
                    "SET version = 99 WHERE id = %s",
                    (str(first.pk),),
                )
            app_connection.rollback()
            _set_local(app_connection, str(rbac_graph.organization_a))
            with pytest.raises(InsufficientPrivilege):
                app_connection.execute(
                    "DELETE FROM clinic_app.intake_demographicscorrection "
                    "WHERE id = %s",
                    (str(receipts[0].pk),),
                )
            app_connection.rollback()

    # The owner role still hits the immutability trigger, so even a
    # migration-time grant creep cannot rewrite history silently. Session
    # scope (is_local=false) survives autocommit statements.
    owner_url = database_url_for_name(
        os.environ["MIGRATION_DATABASE_URL"],
        str(connection.settings_dict["NAME"]),
    )
    with (
        psycopg.connect(owner_url, autocommit=True) as owner,
        owner.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, false)",
            [str(rbac_graph.organization_a)],
        )
        cursor.execute(
            "SELECT count(*) FROM clinic_app.intake_patientdemographics WHERE id = %s",
            [str(first.pk)],
        )
        assert cursor.fetchone() == (1,), "row not visible to owner"
        with pytest.raises(psycopg.errors.CheckViolation) as owner_error:
            cursor.execute(
                "UPDATE clinic_app.intake_patientdemographics "
                "SET version = 99 WHERE id = %s",
                [str(first.pk)],
            )
        assert owner_error.value.diag.message_primary == APPEND_ONLY
        with pytest.raises(psycopg.errors.CheckViolation) as delete_error:
            cursor.execute(
                "DELETE FROM clinic_app.intake_demographicscorrection WHERE id = %s",
                [str(receipts[0].pk)],
            )
        assert delete_error.value.diag.message_primary == APPEND_ONLY


def test_duplicate_identifier_warns_but_never_blocks(
    rbac_graph: RbacGraph,
) -> None:
    services = _services()
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        first_registration = _register(
            services, rbac_graph, full_name="Primeira Pessoa"
        )
        second_registration = _register(
            services, rbac_graph, full_name="Segunda Pessoa"
        )
        services.add_identifier(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=first_registration.enrollment.pk,
            kind="cpf",
            value=_CPF_A,
        )
        outcome = services.add_identifier(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=second_registration.enrollment.pk,
            kind="cpf",
            value=_CPF_A,
        )
        assert outcome.matching_enrollment_id == first_registration.enrollment.pk
        both = services.search_patient_identifiers(
            clinic_id=rbac_graph.clinic_a, kind="cpf", value=_CPF_A
        )
        assert {item.enrollment_id for item in both.items} == {
            first_registration.enrollment.pk,
            second_registration.enrollment.pk,
        }
        # Same patient + same kind is still a conflict, never a second row.
        with pytest.raises(services.IdentifierConflictError):
            services.add_identifier(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=second_registration.enrollment.pk,
                kind="cpf",
                value=_CPF_B,
            )


def test_identifier_retire_releases_the_slot_and_search(
    rbac_graph: RbacGraph,
) -> None:
    services = _services()
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        registration = _register(services, rbac_graph)
        outcome = services.add_identifier(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
            kind="rg",
            value="MG-12.345.678",
        )
        identifier = PatientIdentifier.objects.get(pk=outcome.identifier_id)
        with pytest.raises(services.DemographicsStaleError):
            services.retire_identifier(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=registration.enrollment.pk,
                kind="rg",
                expected_version=identifier.version + 9,
            )
        retired = services.retire_identifier(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
            kind="rg",
            expected_version=identifier.version,
        )
        assert retired.retired_at is not None
        assert retired.version == identifier.version + 1
        # The retired value stays on record: retirement appends a version.
        assert PatientIdentifier.objects.get(pk=identifier.pk).retired_at is None
        gone = services.search_patient_identifiers(
            clinic_id=rbac_graph.clinic_a, kind="rg", value="MG-12.345.678"
        )
        assert gone.items == ()
        re_added = services.add_identifier(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
            kind="rg",
            value="SP-987654",
        )
        re_added_row = PatientIdentifier.objects.get(pk=re_added.identifier_id)
        assert re_added_row.retired_at is None
        assert re_added_row.version == retired.version + 1
        assert [
            row.value
            for row in PatientIdentifier.objects.filter(
                patient_id=registration.patient.pk, kind="rg"
            ).order_by("version")
        ] == ["mg12345678", "mg12345678", "sp987654"]


def test_finance_billing_read_is_minimal_and_write_denied(
    rbac_graph: RbacGraph,
) -> None:
    services = _services()
    finance = User.objects.create(
        username=f"todo17-finance-{uuid4().hex}",
        password=make_password("todo17-finance-credential"),
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user_id=finance.pk,
            role=UserClinicRole.Role.FINANCE,
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        registration = _register(services, rbac_graph)
        services.update_demographics(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
            expected_version=0,
            changes={"legal_name": "Paciente Sintetico", "language": "pt-BR"},
            reason="registration intake",
        )
        services.save_insurance_membership(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
            sequence=1,
            expected_version=0,
            membership={
                "payer_name": "Plano Sintetico",
                "membership_number": "9988",
                "valid_until": date(2030, 12, 31),
            },
        )
    with (
        runtime_role(),
        tenant_context(finance.pk, rbac_graph.organization_a),
    ):
        billing = services.billing_demographics(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
        )
        assert billing.patient_id == registration.patient.pk
        assert billing.birth_date == date(1990, 5, 17)
        assert billing.memberships[0].payer_name == "Plano Sintetico"
        assert billing.memberships[0].valid_until == date(2030, 12, 31)
        with pytest.raises(services.PatientAccessDeniedError):
            services.update_demographics(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=registration.enrollment.pk,
                expected_version=1,
                changes={"occupation": "analista"},
                reason="finance should not write",
            )
        with pytest.raises(services.PatientAccessDeniedError):
            services.demographics_profile(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=registration.enrollment.pk,
            )
        with pytest.raises(services.PatientAccessDeniedError):
            services.search_patient_identifiers(
                clinic_id=rbac_graph.clinic_a, kind="cpf", value=_CPF_A
            )


def test_clinic_policy_requires_fields_and_sentinels_satisfy(
    rbac_graph: RbacGraph,
) -> None:
    services = _services()
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        registration = _register(services, rbac_graph)
        # shared_user is receptionist: clinic configuration is not theirs.
        with pytest.raises(services.PatientAccessDeniedError):
            services.set_intake_policy(
                clinic_id=rbac_graph.clinic_a, required_fields=["social_name"]
            )
    org_admin = User.objects.create(
        username=f"todo17-admin-{uuid4().hex}",
        password=make_password("todo17-admin-credential"),
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user_id=org_admin.pk,
            role=UserClinicRole.Role.CLINIC_ADMIN,
        )
    with (
        runtime_role(),
        tenant_context(org_admin.pk, rbac_graph.organization_a),
    ):
        policy = services.set_intake_policy(
            clinic_id=rbac_graph.clinic_a, required_fields=["social_name"]
        )
        assert policy.version == 1
        assert policy.required_fields == ["social_name"]
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        with pytest.raises(services.DemographicsRequiredError):
            services.update_demographics(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=registration.enrollment.pk,
                expected_version=0,
                changes={"occupation": "dev"},
                reason="missing required",
            )
        # A deliberate non-answer satisfies the requirement; nothing is
        # invented and the stored value stays empty.
        saved = services.update_demographics(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
            expected_version=0,
            changes={"occupation": "dev"},
            unknown={"social_name": "declined"},
            reason="patient declined to give a social name",
        )
        stored = PatientDemographics.objects.get(pk=saved.pk)
        assert stored.social_name == ""
        assert stored.unknown_fields == {"social_name": "declined"}
        # Recording the value later clears the non-answer.
        later = services.update_demographics(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
            expected_version=1,
            changes={"social_name": "Nome Social"},
            reason="patient informed it",
        )
        stored = PatientDemographics.objects.get(pk=later.pk)
        assert stored.social_name == "Nome Social"
        assert stored.unknown_fields is None
        # A value and a reason for the same field are contradictory.
        with pytest.raises(services.DemographicsInputError):
            services.update_demographics(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=registration.enrollment.pk,
                expected_version=2,
                changes={"pronouns": "ela/dela"},
                unknown={"pronouns": "not_informed"},
                reason="",
            )


def test_carry_forward_marks_patient_reported_unverified(
    rbac_graph: RbacGraph,
) -> None:
    services = _services()
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        registration = _register(services, rbac_graph)
        invitation = services.issue_invitation(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
        )
    # Templates publish under a clinic admin role, matching the real flow.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user_id=rbac_graph.clinic_admin,
            role=UserClinicRole.Role.CLINIC_ADMIN,
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        template = services.publish_template(
            clinic_id=rbac_graph.clinic_a,
            key="intake",
            title="Intake",
            questions=[
                {
                    "id": "q_social_name",
                    "label": "Nome social",
                    "type": "text",
                    "required": True,
                    "max_length": 160,
                    "options": [],
                },
                {
                    "id": "q_notes",
                    "label": "Observacoes",
                    "type": "text",
                    "required": False,
                    "max_length": 160,
                    "options": [],
                },
            ],
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        response = services.assign_questionnaire(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
            template_id=template.pk,
        )
    with runtime_role():
        session = redeem_invitation(rbac_graph.clinic_a, invitation.secret)
    assert session is not None
    with runtime_role(), patient_session_context(session):
        submitted = services.submit_intake(
            response_id=response.pk,
            answers={
                "q_social_name": "Nome Social",
                "q_notes": "not a demographic field",
            },
            expected_revision=1,
        )
        assert submitted.state == "submitted"
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        # Reception can write demographics but cannot read the patient
        # questionnaire itself; nothing carries for them.
        assert (
            services.carry_forward_demographics(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=registration.enrollment.pk,
                reason="carried from submitted questionnaire",
            )
            is None
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        carried = services.carry_forward_demographics(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
            reason="carried from submitted questionnaire",
        )
        assert carried is not None
        assert carried.source == "patient_reported"
        assert carried.social_name == "Nome Social"
        profile = services.demographics_profile(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
        )
        assert profile.source == "patient_reported"
        assert profile.corrections[0].source == "patient_reported"


def test_name_search_folds_diacritics_and_prefers_social_name(
    rbac_graph: RbacGraph,
) -> None:
    services = _services()
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        services.update_demographics(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=_register(
                services, rbac_graph, full_name="Joao da Silva"
            ).enrollment.pk,
            expected_version=0,
            changes={"legal_name": "Joao da Silva"},
            reason="registration intake",
        )
        registration = _register(services, rbac_graph, full_name="João Gonçalves")
        services.update_demographics(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
            expected_version=0,
            changes={
                "legal_name": "João Gonçalves",
                "social_name": "Joana Gonçalves",
            },
            reason="registration intake",
        )
        folded = services.search_patients(
            clinic_id=rbac_graph.clinic_a, query="joao goncalves", page=1
        )
        assert [item.enrollment_id for item in folded.items] == [
            registration.enrollment.pk
        ]
        assert folded.items[0].display_name == "Joana Gonçalves"
        # The social name is searchable too, with the same fold.
        by_social = services.search_patients(
            clinic_id=rbac_graph.clinic_a, query="joana goncalves", page=1
        )
        assert [item.enrollment_id for item in by_social.items] == [
            registration.enrollment.pk
        ]
        accented = services.search_patients(
            clinic_id=rbac_graph.clinic_a, query="João Gonçalves", page=1
        )
        assert [item.enrollment_id for item in accented.items] == [
            registration.enrollment.pk
        ]


def test_addresses_contacts_memberships_version_and_retire(
    rbac_graph: RbacGraph,
) -> None:
    services = _services()
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        registration = _register(services, rbac_graph)
        address = services.save_patient_address(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
            kind="home",
            expected_version=0,
            address={
                "postal_code": "01310-100",
                "street": "Av. Paulista",
                "street_number": "1000",
                "district": "Bela Vista",
                "city": "São Paulo",
                "state_code": "SP",
            },
        )
        assert address.version == 1
        with pytest.raises(services.DemographicsInputError):
            services.save_patient_address(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=registration.enrollment.pk,
                kind="work",
                expected_version=0,
                address={"postal_code": "123", "state_code": "SP"},
            )
        contact = services.save_emergency_contact(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
            sequence=1,
            expected_version=0,
            contact={
                "name": "Contato Emergencia",
                "relationship": "irma",
                "phone": "+55 (11) 98888-7777",
            },
        )
        assert contact.phone == "+5511988887777"
        with pytest.raises(services.DemographicsStaleError):
            services.save_emergency_contact(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=registration.enrollment.pk,
                sequence=1,
                expected_version=0,
                contact={"name": "Outro", "phone": "+5511999990000"},
            )
        profile = services.demographics_profile(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
        )
        assert profile.addresses[0].postal_code == "01310100"
        assert profile.emergency_contacts[0].phone == "+5511988887777"
        retired = services.save_patient_address(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
            kind="home",
            expected_version=1,
            address=None,
        )
        assert retired.retired_at is not None
        assert retired.version == 2
        profile = services.demographics_profile(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
        )
        assert profile.addresses == ()
        assert "home" in profile.free_address_kinds
        # Replaced values stay on record as earlier versions.
        history = list(
            PatientAddress.objects.filter(
                patient_id=registration.patient.pk, kind="home"
            ).order_by("version")
        )
        assert [
            (row.version, row.street, row.retired_at is None) for row in history
        ] == [
            (1, "Av. Paulista", True),
            (2, "", False),
        ]
        # Editing a current row appends the next version; 0 re-creates.
        replaced = services.save_emergency_contact(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
            sequence=1,
            expected_version=1,
            contact={"name": "Contato Novo", "phone": "11977776666"},
        )
        assert replaced.version == 2
        assert [
            row.name
            for row in EmergencyContact.objects.filter(
                patient_id=registration.patient.pk
            ).order_by("version")
        ] == ["Contato Emergencia", "Contato Novo"]
        re_created = services.save_patient_address(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
            kind="home",
            expected_version=0,
            address={"city": "Campinas", "state_code": "sp"},
        )
        assert (re_created.version, re_created.state_code) == (3, "SP")


def test_new_tables_isolate_tenants(tenant_probe_pair: RbacGraph) -> None:
    services = _services()
    graph = tenant_probe_pair
    org_b_patient = _org_b_patient(graph)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(graph.organization_b)],
        )
        org_b_version = PatientDemographics.objects.create(
            organization_id=graph.organization_b,
            patient_id=org_b_patient.pk,
            clinic_id=graph.clinic_c,
            enrollment_id=PatientClinicEnrollment.objects.get(
                organization_id=graph.organization_b,
                patient_id=org_b_patient.pk,
            ).pk,
            version=1,
            legal_name="Org B Paciente",
        )
        DemographicsCorrection.objects.create(
            organization_id=graph.organization_b,
            clinic_id=graph.clinic_c,
            patient_id=org_b_patient.pk,
            demographics=org_b_version,
            actor_id=graph.shared_user,
            actor_label="org-b-seed",
            changed_fields=["legal_name"],
        )
    with (
        runtime_role(),
        tenant_context(graph.shared_user, graph.organization_a),
    ):
        registration = _register(services, graph)
        services.update_demographics(
            clinic_id=graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
            expected_version=0,
            changes={"legal_name": "Org A Paciente"},
            reason="registration intake",
        )
    assert_no_cross_tenant_rows(graph, PatientDemographics)
    assert_no_cross_tenant_rows(graph, DemographicsCorrection)


# One harmless SET clause per identity table (receipts carry no version).
_TAMPER_SET: dict[str, str] = {
    "intake_patientdemographics": "version = 99",
    "intake_demographicscorrection": "reason = NULL",
    "intake_patientidentifier": "version = 99",
    "intake_patientaddress": "version = 99",
    "intake_emergencycontact": "version = 99",
    "intake_insurancemembership": "version = 99",
    "intake_clinicintakepolicy": "version = 99",
}


def test_identity_tables_refuse_raw_tampering(rbac_graph: RbacGraph) -> None:
    """SC-2/SC-5: history transitions are enforced by the database itself.

    The runtime role cannot rewrite or delete any identity row; the owner
    role hits the append-only trigger; versions must be dense; retirements
    need an active version; every demographics version needs a receipt that
    names the version it superseded; the registry row changes only with a
    new demographics version in the same transaction.
    """
    services = _services()
    organization = rbac_graph.organization_a
    clinic = rbac_graph.clinic_a
    with runtime_role(), tenant_context(rbac_graph.shared_user, organization):
        registration = _register(services, rbac_graph)
        enrollment = registration.enrollment.pk
        first = services.update_demographics(
            clinic_id=clinic,
            enrollment_id=enrollment,
            expected_version=0,
            changes={"social_name": "Social Um"},
            reason="",
        )
        rows = {
            "intake_patientdemographics": first.pk,
            "intake_patientidentifier": services.add_identifier(
                clinic_id=clinic, enrollment_id=enrollment, kind="cpf", value=_CPF_A
            ).identifier_id,
            "intake_patientaddress": services.save_patient_address(
                clinic_id=clinic,
                enrollment_id=enrollment,
                kind="home",
                expected_version=0,
                address={"city": "Recife", "state_code": "PE"},
            ).pk,
            "intake_emergencycontact": services.save_emergency_contact(
                clinic_id=clinic,
                enrollment_id=enrollment,
                sequence=1,
                expected_version=0,
                contact={"name": "Contato", "phone": "81988887777"},
            ).pk,
            "intake_insurancemembership": services.save_insurance_membership(
                clinic_id=clinic,
                enrollment_id=enrollment,
                sequence=1,
                expected_version=0,
                membership={"payer_name": "Operadora"},
            ).pk,
            "intake_demographicscorrection": DemographicsCorrection.objects.get(
                demographics_id=first.pk
            ).pk,
        }
    manager = _role_user(rbac_graph, UserClinicRole.Role.CLINIC_MANAGER)
    with runtime_role(), tenant_context(manager, organization):
        rows["intake_clinicintakepolicy"] = services.set_intake_policy(
            clinic_id=clinic, required_fields=[]
        ).pk
    assert set(rows) == set(_TAMPER_SET)
    with _database("APP_DATABASE_URL", organization) as app:
        for table, row_id in rows.items():
            for statement in (
                f"UPDATE clinic_app.{table} SET {_TAMPER_SET[table]} WHERE id = %s",  # noqa: S608 - fixed tables
                f"DELETE FROM clinic_app.{table} WHERE id = %s",  # noqa: S608 - fixed tables
            ):
                with pytest.raises(InsufficientPrivilege):
                    app.execute(statement, [str(row_id)])
        # The runtime role holds UPDATE on the registry name, but the guard
        # admits it only together with a new demographics version.
        with pytest.raises(CheckViolation) as mirror:
            app.execute(
                "UPDATE clinic_app.intake_patient SET full_name = full_name "
                "WHERE id = %s",
                [str(registration.patient.pk)],
            )
        assert mirror.value.diag.message_primary == (
            "patient registry identity changes only with a demographics version"
        )
    with _database("MIGRATION_DATABASE_URL", organization) as owner:
        for table, row_id in rows.items():
            for statement in (
                f"UPDATE clinic_app.{table} SET {_TAMPER_SET[table]} WHERE id = %s",  # noqa: S608 - fixed tables
                f"DELETE FROM clinic_app.{table} WHERE id = %s",  # noqa: S608 - fixed tables
            ):
                with pytest.raises(CheckViolation) as refused:
                    owner.execute(statement, [str(row_id)])
                assert refused.value.diag.message_primary == APPEND_ONLY
        with pytest.raises(CheckViolation) as skipped:
            owner.execute(
                "INSERT INTO clinic_app.intake_patientaddress "
                "(id, organization_id, patient_id, kind, version, created_at) "
                "VALUES (gen_random_uuid(), %s, %s, 'home', 9, now())",
                [str(organization), str(registration.patient.pk)],
            )
        assert skipped.value.diag.message_primary == (
            "identity versions must follow the latest version"
        )
        with pytest.raises(CheckViolation) as orphan:
            owner.execute(
                "INSERT INTO clinic_app.intake_patientaddress "
                "(id, organization_id, patient_id, kind, version, retired_at,"
                " created_at) "
                "VALUES (gen_random_uuid(), %s, %s, 'work', 1, now(), now())",
                [str(organization), str(registration.patient.pk)],
            )
        assert orphan.value.diag.message_primary == (
            "only an active identity version can be retired"
        )
    # Receipt binding: a version without its receipt fails at commit, and a
    # receipt must name the version it superseded.
    with (  # noqa: PT012 - the commit is what raises
        pytest.raises(IntegrityError) as missing_receipt,
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization)],
        )
        PatientDemographics.objects.create(
            organization_id=organization,
            patient_id=registration.patient.pk,
            clinic_id=clinic,
            enrollment_id=enrollment,
            version=2,
            social_name="Social Dois",
        )
    assert "needs its correction receipt" in str(missing_receipt.value)
    with (  # noqa: PT012 - the commit is what raises
        pytest.raises(IntegrityError) as wrong_previous,
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization)],
        )
        second = PatientDemographics.objects.create(
            organization_id=organization,
            patient_id=registration.patient.pk,
            clinic_id=clinic,
            enrollment_id=enrollment,
            version=2,
            social_name="Social Dois",
        )
        DemographicsCorrection.objects.create(
            organization_id=organization,
            clinic_id=clinic,
            patient_id=registration.patient.pk,
            demographics=second,
            previous=None,
            actor_id=rbac_graph.shared_user,
            actor_label="tamper",
            changed_fields=[],
        )
    assert "must name the superseded version" in str(wrong_previous.value)


def test_identifier_search_shows_the_current_name_after_corrections(
    rbac_graph: RbacGraph,
) -> None:
    """B4: an exact document hit shows the latest version, never an old one."""
    services = _services()
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        registration = _register(services, rbac_graph)
        enrollment = registration.enrollment.pk
        services.add_identifier(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=enrollment,
            kind="passport",
            value="BR123456",
        )
        for version, social in enumerate(
            ("Sintetico Antigo", "Sintetico Meio", "Sintetico Atual")
        ):
            services.update_demographics(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=enrollment,
                expected_version=version,
                changes={"social_name": social},
                reason="",
            )
        profile = services.demographics_profile(
            clinic_id=rbac_graph.clinic_a, enrollment_id=enrollment
        )
        page = services.search_patient_identifiers(
            clinic_id=rbac_graph.clinic_a, kind="passport", value="br 123456"
        )
        assert profile.version == 3
        assert [item.display_name for item in page.items] == ["Sintetico Atual"]
        assert page.items[0].display_name == profile.display_name
        by_name = services.search_patients(
            clinic_id=rbac_graph.clinic_a, query="Sintetico Atual", page=1
        )
        assert [item.display_name for item in by_name.items] == ["Sintetico Atual"]


def test_registration_needs_only_a_name_and_records_non_answers(
    rbac_graph: RbacGraph,
) -> None:
    """B2: no invented name or date; declined/unknown are real answers."""
    services = _services()
    organization = rbac_graph.organization_a
    clinic = rbac_graph.clinic_a
    with runtime_role(), tenant_context(rbac_graph.shared_user, organization):
        outcome = services.register_patient(
            clinic_id=clinic,
            idempotency_key=uuid4(),
            legal_name="",
            social_name="Ariel Sintetica",
            birth_date=None,
            unknown={"legal_name": "declined", "birth_date": "not_informed"},
        )
        patient = Patient.objects.get(pk=outcome.registration.patient.pk)
        assert patient.full_name == "Ariel Sintetica"
        assert patient.birth_date is None
        enrollment = outcome.registration.enrollment.pk
        profile = services.demographics_profile(
            clinic_id=clinic, enrollment_id=enrollment
        )
        assert profile.values["legal_name"] == ""
        assert profile.birth_date is None
        assert profile.unknown == {
            "legal_name": "declined",
            "birth_date": "not_informed",
        }
        declined = services.update_demographics(
            clinic_id=clinic,
            enrollment_id=enrollment,
            expected_version=1,
            changes={"sex_at_birth": "declined", "gender_identity": "declined"},
            reason="patient preferred not to answer",
        )
        stored = PatientDemographics.objects.get(pk=declined.pk)
        assert (stored.sex_at_birth, stored.gender_identity) == ("declined", "declined")
        # A cleared legal name stays cleared: the registry follows the
        # social name instead of silently keeping the old legal name.
        services.update_demographics(
            clinic_id=clinic,
            enrollment_id=enrollment,
            expected_version=2,
            changes={"legal_name": "Ariel Civil", "birth_date": date(2001, 2, 3)},
            reason="documents presented",
        )
        corrected = Patient.objects.get(pk=patient.pk)
        assert (corrected.full_name, corrected.birth_date) == (
            "Ariel Civil",
            date(2001, 2, 3),
        )
        services.update_demographics(
            clinic_id=clinic,
            enrollment_id=enrollment,
            expected_version=3,
            changes={"legal_name": ""},
            reason="legal name entered by mistake",
        )
        patient.refresh_from_db()
        assert patient.full_name == "Ariel Sintetica"
        with pytest.raises(services.DemographicsNameRequiredError):
            services.update_demographics(
                clinic_id=clinic,
                enrollment_id=enrollment,
                expected_version=4,
                changes={"social_name": ""},
                reason="",
            )
        with pytest.raises(services.DemographicsNameRequiredError):
            services.register_patient(
                clinic_id=clinic,
                idempotency_key=uuid4(),
                legal_name="",
                social_name="",
                birth_date=None,
            )
    admin = _role_user(rbac_graph, UserClinicRole.Role.CLINIC_ADMIN)
    with runtime_role(), tenant_context(admin, organization):
        services.set_intake_policy(
            clinic_id=clinic, required_fields=["birth_date", "language"]
        )
        # Registration keeps its legacy authority: a clinic admin may
        # register, and the policy applies to registration too.
        with pytest.raises(services.DemographicsRequiredError):
            services.register_patient(
                clinic_id=clinic,
                idempotency_key=uuid4(),
                legal_name="Beatriz Sintetica",
                social_name="",
                birth_date=None,
            )
        registered = services.register_patient(
            clinic_id=clinic,
            idempotency_key=uuid4(),
            legal_name="Beatriz Sintetica",
            social_name="",
            birth_date=None,
            changes={"language": "Libras"},
            unknown={"birth_date": "declined"},
            identifier_kind="cpf",
            identifier_value=_CPF_B,
        )
        assert registered.registration.patient.birth_date is None
        assert registered.matching_enrollment_id is None


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        (UserClinicRole.Role.OWNER, True),
        (UserClinicRole.Role.ORG_ADMIN, True),
        (UserClinicRole.Role.CLINIC_ADMIN, True),
        (UserClinicRole.Role.CLINIC_MANAGER, True),
        (UserClinicRole.Role.FINANCE, True),
        (UserClinicRole.Role.RECEPTIONIST, False),
        (UserClinicRole.Role.SCHEDULER, False),
        (UserClinicRole.Role.PHYSICIAN, False),
        (UserClinicRole.Role.NURSE, False),
        (UserClinicRole.Role.ALLIED_PROFESSIONAL, False),
    ],
)
def test_intake_policy_is_clinic_configuration(
    rbac_graph: RbacGraph, role: UserClinicRole.Role, allowed: bool
) -> None:
    """B8: RP Config/templates - manager/finance clinic, org admin org-wide."""
    services = _services()
    actor = _role_user(rbac_graph, role)
    with runtime_role(), tenant_context(actor, rbac_graph.organization_a):
        if allowed:
            policy = services.set_intake_policy(
                clinic_id=rbac_graph.clinic_a, required_fields=["language"]
            )
            assert policy.required_fields == ["language"]
        else:
            with pytest.raises(services.PatientAccessDeniedError):
                services.set_intake_policy(
                    clinic_id=rbac_graph.clinic_a, required_fields=["language"]
                )


def _page(client: object, url: str, data: dict[str, str]) -> tuple[str, Document]:
    with runtime_role():
        response = client.post(url, data)  # type: ignore[attr-defined]
    assert response.status_code == 200, response.status_code
    return response.content.decode(), Document(response.content)


def test_staff_surface_edits_every_section_in_pt_br(rbac_graph: RbacGraph) -> None:
    """B3/B6/B7: legal name, birth date, addresses, contacts and plans are
    editable through the real views; every label and choice is translated;
    both scrollable tables are named, keyboard-focusable regions."""
    client, receptionist = receptionist_client(rbac_graph)
    clinic = rbac_graph.clinic_a
    with runtime_role():
        created = client.post(
            f"/intake/clinics/{clinic}/patients/new/",
            {
                "full_name": "",
                "full_name_status": "declined",
                "social_name": "Dara Sintetica",
                "birth_date": "",
                "birth_date_status": "not_informed",
                "idempotency_key": str(uuid4()),
            },
        )
    assert created.status_code == 303
    with runtime_role(), tenant_context(receptionist.pk, rbac_graph.organization_a):
        enrollment = PatientClinicEnrollment.objects.filter(clinic_id=clinic).latest(
            "created_at"
        )
    url = reverse("intake:patient-demographics", args=(clinic,))
    base = {"enrollment_id": str(enrollment.pk)}
    html, document = _page(client, url, {**base, "action": "manage"})
    for element_id in (
        "id_legal_name",
        "id_birth_date",
        "id_legal_name_status",
        "address-new-postal_code",
        "contact-new-phone",
        "membership-new-payer_name",
    ):
        assert document.attributes_for(element_id), element_id
    assert gettext("Declined to answer") in html
    assert ">declined<" not in html
    assert ">not_informed<" not in html

    html, _ = _page(
        client,
        url,
        {
            **base,
            "action": "save",
            "expected_version": "1",
            "legal_name": "Dara Civil",
            "social_name": "Dara Sintetica",
            "birth_date": "1999-09-09",
            "sex_at_birth": "declined",
            "reason": "documentos apresentados",
        },
    )
    assert gettext("Demographics saved.") in html
    for action, fields in (
        (
            "save_address",
            {"kind": "home", "postal_code": "50000-000", "city": "Recife"},
        ),
        (
            "save_contact",
            {"sequence": "1", "name": "Contato Um", "phone": "81999990000"},
        ),
        (
            "save_membership",
            {"sequence": "1", "payer_name": "Operadora Sintetica", "valid_until": ""},
        ),
    ):
        html, _ = _page(
            client,
            url,
            {
                **base,
                "action": action,
                "slot": "new",
                "expected_version": "0",
                **fields,
            },
        )
        assert gettext("Record saved.") in html, action
    html, document = _page(client, url, {**base, "action": "manage"})
    assert document.attributes_for("address-home-city")["value"] == "Recife"
    assert document.attributes_for("contact-1-name")["value"] == "Contato Um"
    assert document.attributes_for("membership-1-payer_name")["value"] == (
        "Operadora Sintetica"
    )
    html, document = _page(
        client,
        url,
        {
            **base,
            "action": "save_address",
            "kind": "home",
            "expected_version": "1",
            "postal_code": "50000000",
            "city": "Olinda",
        },
    )
    assert document.attributes_for("address-home-city")["value"] == "Olinda"
    html, document = _page(
        client,
        url,
        {**base, "action": "retire_contact", "sequence": "1", "expected_version": "1"},
    )
    assert gettext("Record removed. Earlier versions stay in history.") in html
    assert "contact-1-name" not in document.identifiers()

    corrections = html[html.index('id="patient-correction-history"') :]
    assert gettext("Legal name") in corrections
    assert "legal_name" not in corrections
    assert "social_name" not in corrections
    regions = [
        attributes
        for attributes in document.tagged("div")
        if attributes.get("role") == "region"
    ]
    assert [region.get("aria-labelledby") for region in regions] == [
        "corrections-caption"
    ]
    assert all(region.get("tabindex") == "0" for region in regions)
    document.assert_unique_identifiers()
    document.assert_descriptions_resolve()
    document.assert_every_control_is_labelled()
    document.assert_every_form_is_post_with_csrf()


def test_protected_migration_is_non_atomic_and_irreversible() -> None:
    """B1/SC-5/SC-17: rollback = restore; unapply refuses before any SQL."""
    migration = importlib.import_module(
        "apps.intake.migrations.0012_patient_demographics"
    ).Migration
    steps = [
        operation
        for operation in migration.operations
        if isinstance(operation, RunPython)
    ]
    assert migration.atomic is False
    assert steps
    assert all(step.atomic is True and not step.reversible for step in steps)
