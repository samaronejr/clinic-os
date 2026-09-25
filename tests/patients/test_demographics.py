"""Todo 17: demographics versions, blind-indexed identifiers and history.

Covers the plan's acceptance rows: strict CPF check-digit rejection,
blind-index exact search with org isolation, append-only version history
with ``demographics_stale`` conflicts, permission-scoped access (finance
gets only the billing read), deliberate 'unknown/declined' values, the
clinic-required-field policy and patient-reported carry-forward.
"""

from __future__ import annotations

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
    Patient,
    PatientClinicEnrollment,
    PatientDemographics,
    PatientIdentifier,
)
from apps.intake.patient_access import patient_session_context, redeem_invitation
from apps.tenancy.db import tenant_context
from apps.tenancy.envelope import BlindIndex, blind_indexes
from django.contrib.auth.hashers import make_password
from django.db import connection, transaction
from psycopg.errors import InsufficientPrivilege

from database_urls import database_url_for_name
from patient_service_support import runtime_role
from tenant_probe_support import assert_no_cross_tenant_rows

if TYPE_CHECKING:
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
        assert (
            owner_error.value.diag.message_primary
            == "patient demographics versions are append-only"
        )
        with pytest.raises(psycopg.errors.CheckViolation) as delete_error:
            cursor.execute(
                "DELETE FROM clinic_app.intake_demographicscorrection WHERE id = %s",
                [str(receipts[0].pk)],
            )
        assert (
            delete_error.value.diag.message_primary
            == "patient demographics versions are append-only"
        )


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
        assert (
            PatientIdentifier.objects.get(pk=re_added.identifier_id).retired_at is None
        )


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
        # shared_user is receptionist: staff.clinic is not in the bundle.
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
        saved = services.update_demographics(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
            expected_version=0,
            changes={"social_name": "not_informed"},
            reason="patient declined to give a social name",
        )
        assert saved.social_name == "not_informed"


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
        profile = services.demographics_profile(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
        )
        assert profile.addresses == ()


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
