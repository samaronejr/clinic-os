"""Behavioral questionnaire acceptance under the real clinic_app role and RLS."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.current_context import CurrentActorError
from apps.identity.models import User, UserClinicRole
from apps.intake.models import (
    QuestionnaireEvent,
    QuestionnaireResponse,
    QuestionnaireTemplate,
)
from apps.intake.patient_access import (
    PATIENT_SESSION_KEY,
    patient_session_context,
    redeem_invitation,
)
from apps.intake.questionnaires import (
    assign_for_appointment,
    assign_questionnaire,
    clinical_response,
    completion_status,
    patient_response,
    publish_template,
    published_templates,
    reopen_response,
    save_response,
    validate_answers,
    validate_questions,
)
from apps.intake.services import (
    PatientAccessDeniedError,
    create_patient,
    issue_invitation,
    submit_intake,
)
from apps.tenancy.db import tenant_context
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.test import Client

from otp_test_support import (
    create_totp_device,
    fixed_otp_time,
    token_for,
)
from otp_test_support import (
    runtime_role as http_runtime_role,
)
from patient_service_support import runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL
from renewal.test_encounters import seed as encounter_seed

if TYPE_CHECKING:
    from uuid import UUID

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

QUESTIONS = [
    {
        "id": "q_text",
        "label": "Informações para a consulta",
        "type": "text",
        "required": True,
        "max_length": 40,
        "options": [],
    },
    {
        "id": "q_choice",
        "label": "Preferência de contato",
        "type": "selection",
        "required": True,
        "max_length": 20,
        "options": ["Telefone", "Mensagem"],
    },
    {
        "id": "q_bool",
        "label": "Deseja conversar?",
        "type": "boolean",
        "required": True,
        "max_length": 5,
        "options": [],
    },
]
ANSWERS = {
    "q_text": "Informação sintética privada",
    "q_choice": "Mensagem",
    "q_bool": False,
}


def seed(graph: RbacGraph) -> tuple[QuestionnaireResponse, UUID]:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [str(graph.organization_a)],
        )
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=graph.clinic_admin,
            role="clinic_admin",
        )
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        registration = create_patient(
            clinic_id=graph.clinic_a,
            full_name="Paciente Sintético Formulário",
            birth_date=date(1990, 1, 1),
            idempotency_key=uuid4(),
        )
        invitation = issue_invitation(
            clinic_id=graph.clinic_a, enrollment_id=registration.enrollment.pk
        )
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        template = publish_template(
            clinic_id=graph.clinic_a,
            key="preconsulta",
            title="Pré-consulta sintética",
            questions=QUESTIONS,
        )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        response = assign_questionnaire(
            clinic_id=graph.clinic_a,
            enrollment_id=registration.enrollment.pk,
            template_id=template.pk,
        )
    with runtime_role():
        session = redeem_invitation(graph.clinic_a, invitation.secret)
    assert session is not None
    return response, session


def test_draft_resume_version_change_submit_and_clinical_reopen(
    rbac_graph: RbacGraph,
) -> None:
    response, session = seed(rbac_graph)
    with runtime_role(), patient_session_context(session):
        saved = save_response(
            response_id=response.pk,
            answers={"q_text": ANSWERS["q_text"]},
            expected_revision=1,
        )
        assert saved.state == "draft"
        assert saved.revision == 2
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        new = publish_template(
            clinic_id=rbac_graph.clinic_a,
            key="preconsulta",
            title="Versão nova",
            questions=QUESTIONS,
        )
        assert new.version == 2
    with runtime_role(), patient_session_context(session):
        resumed = patient_response(response.pk)
        assert resumed.template.version == 1
        assert resumed.answers == {"q_text": ANSWERS["q_text"]}
        submitted = submit_intake(
            response_id=response.pk, answers=ANSWERS, expected_revision=2
        )
        assert submitted.state == "submitted"
        assert submitted.submitted_at is not None
        with pytest.raises(ValidationError):
            save_response(response_id=response.pk, answers={}, expected_revision=3)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        viewed = clinical_response(
            clinic_id=rbac_graph.clinic_a, response_id=response.pk
        )
        assert viewed.answers == ANSWERS
        reopened = reopen_response(
            clinic_id=rbac_graph.clinic_a,
            response_id=response.pk,
            reason="Correção solicitada pelo paciente",
            expected_revision=3,
        )
        assert reopened.state == "draft"
        history = list(
            QuestionnaireEvent.objects.filter(response_id=response.pk).order_by(
                "revision"
            )
        )
        assert [event.action for event in history] == [
            "assigned",
            "draft_saved",
            "submitted",
            "reopened",
        ]
        assert history[2].answers == ANSWERS
        assert history[2].patient_session_id == session
        assert history[3].actor_id == rbac_graph.physician
        assert history[3].reason
    with runtime_role(), patient_session_context(session):
        resubmitted = submit_intake(
            response_id=response.pk,
            answers={**ANSWERS, "q_bool": True},
            expected_revision=4,
        )
        assert resubmitted.revision == 5
        assert resubmitted.template_id == response.template_id


@pytest.mark.parametrize(
    "answers",
    [
        {},
        {**ANSWERS, "q_text": "x" * 41},
        {**ANSWERS, "q_choice": "Other"},
        {**ANSWERS, "q_bool": "false"},
        {**ANSWERS, "q_bool": 0},
        {**ANSWERS, "q_text": "   "},
        {**ANSWERS, "forged": "value"},
    ],
)
def test_invalid_submit_is_atomic(rbac_graph: RbacGraph, answers: object) -> None:
    response, session = seed(rbac_graph)
    with runtime_role(), patient_session_context(session):
        with pytest.raises(ValidationError):
            submit_intake(response_id=response.pk, answers=answers, expected_revision=1)
        retained = patient_response(response.pk)
        assert retained.answers == {}
        assert retained.revision == 1
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        assert QuestionnaireEvent.objects.filter(response_id=response.pk).count() == 1


def test_stale_editor_and_patient_reopen_denied(rbac_graph: RbacGraph) -> None:
    response, session = seed(rbac_graph)
    with runtime_role(), patient_session_context(session):
        save_response(response_id=response.pk, answers={}, expected_revision=1)
        with pytest.raises(ValidationError):
            submit_intake(response_id=response.pk, answers=ANSWERS, expected_revision=1)
        with pytest.raises(CurrentActorError):
            reopen_response(
                clinic_id=rbac_graph.clinic_a,
                response_id=response.pk,
                reason="Não autorizado",
                expected_revision=2,
            )
        with pytest.raises(PatientAccessDeniedError):
            patient_response(uuid4())


def test_reception_only_status_other_clinic_and_unbound_denied(
    rbac_graph: RbacGraph,
) -> None:
    response, session = seed(rbac_graph)
    with runtime_role(), patient_session_context(session):
        submit_intake(response_id=response.pk, answers=ANSWERS, expected_revision=1)
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        assert completion_status(
            clinic_id=rbac_graph.clinic_a, enrollment_id=response.enrollment_id
        ) == [(response.pk, "submitted", 2)]
        assert not QuestionnaireResponse.objects.exists()
        assert not QuestionnaireEvent.objects.exists()
        with pytest.raises(CurrentActorError):
            clinical_response(clinic_id=rbac_graph.clinic_a, response_id=response.pk)
        other = create_patient(
            clinic_id=rbac_graph.clinic_a,
            full_name="Outro Sintético",
            birth_date=date(1990, 1, 1),
            idempotency_key=uuid4(),
        )
        code = issue_invitation(
            clinic_id=rbac_graph.clinic_a, enrollment_id=other.enrollment.pk
        )
    with runtime_role():
        other_session = redeem_invitation(rbac_graph.clinic_a, code.secret)
    assert other_session is not None
    with runtime_role(), patient_session_context(other_session):
        assert not QuestionnaireResponse.objects.exists()
        assert not QuestionnaireTemplate.objects.exists()
        with pytest.raises(PatientAccessDeniedError):
            patient_response(response.pk)
        assert (
            completion_status(
                clinic_id=rbac_graph.clinic_a, enrollment_id=response.enrollment_id
            )
            == []
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_b),
    ):
        assert not QuestionnaireResponse.objects.exists()
        assert (
            completion_status(
                clinic_id=rbac_graph.clinic_a, enrollment_id=response.enrollment_id
            )
            == []
        )
    with runtime_role(), pytest.raises(PatientAccessDeniedError):
        patient_response(response.pk)


def test_database_immutability_and_revoked_operation(rbac_graph: RbacGraph) -> None:
    response, session = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        QuestionnaireTemplate.objects.filter(pk=response.template_id).update(
            title="Overwrite"
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        with pytest.raises(DatabaseError), transaction.atomic():
            QuestionnaireResponse.objects.filter(pk=response.pk).update(
                template_id=uuid4()
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            QuestionnaireEvent.objects.filter(response_id=response.pk).delete()
        with pytest.raises(PatientAccessDeniedError):
            assign_questionnaire(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=response.enrollment_id,
                template_id=response.template_id,
                appointment_id=uuid4(),
            )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        cursor.execute(
            "UPDATE clinic_app.intake_patientsession "
            "SET operations = ARRAY['enrollment_view'] WHERE id = %s",
            [session],
        )
    with (
        runtime_role(),
        patient_session_context(session),
        pytest.raises(PatientAccessDeniedError),
    ):
        patient_response(response.pk)


@pytest.mark.parametrize(
    "mutation",
    ["expression", "duplicate", "type", "required", "length", "options", "count"],
)
def test_closed_configuration_limits(mutation: str) -> None:
    questions = deepcopy(QUESTIONS)
    if mutation == "expression":
        questions[0]["expression"] = "eval(input)"
    elif mutation == "duplicate":
        questions.append(questions[0])
    elif mutation == "type":
        questions[0]["type"] = "script"
    elif mutation == "required":
        questions[0]["required"] = "yes"
    elif mutation == "length":
        questions[0]["max_length"] = 4001
    elif mutation == "options":
        questions[1]["options"] = ["a"] * 31
    else:
        questions = []
    with pytest.raises(ValidationError):
        validate_questions(questions)


def _patient_client(session_id: UUID, *, csrf: bool = False) -> Client:
    client = Client(enforce_csrf_checks=csrf)
    cookie = client.session
    cookie[PATIENT_SESSION_KEY] = str(session_id)
    cookie.save()
    return client


def test_patient_http_save_submit_errors_and_csrf(rbac_graph: RbacGraph) -> None:
    response, session = seed(rbac_graph)
    client = _patient_client(session)
    url = "/patient/questionnaires/"
    payload = {"response_id": str(response.pk), "revision": "1"}
    with http_runtime_role():
        page = client.get(url)
        assert page.status_code == 200
        assert "no-store" in page.headers["Cache-Control"]
        assert (
            client.post(url, {**payload, "action": "open"}).context["response"].pk
            == response.pk
        )
        invalid = client.post(url, {**payload, "action": "submit"})
        assert invalid.context["form"].errors
        invalid = client.post(url, {**payload, "action": "save", "q_text": "x" * 41})
        assert "q_text" in invalid.context["form"].errors
        saved = client.post(url, {**payload, "action": "save", "q_text": "Synthetic"})
        assert saved.context["response"].revision == 2
        stale = client.post(url, {**payload, "action": "save"})
        assert stale.context["form"].non_field_errors()
        sent = client.post(
            url,
            {
                **payload,
                "revision": "2",
                "action": "submit",
                "q_text": "Synthetic",
                "q_choice": "Mensagem",
                "q_bool": "False",
            },
        )
        assert sent.context["response"].state == "submitted"
        assert sent.context["response"].answers["q_bool"] is False
        assert client.post(url, {**payload, "action": "delete"}).status_code == 403
        assert (
            client.post(url, {"response_id": "invalid", "action": "open"}).status_code
            == 403
        )
        assert (
            client.post(
                url, {"response_id": str(uuid4()), "action": "open"}
            ).status_code
            == 403
        )
        assert client.put(url).status_code == 405
        assert _patient_client(session, csrf=True).post(url, payload).status_code == 403


def test_staff_http_totp_inspection_and_reopen(rbac_graph: RbacGraph) -> None:
    response, session = seed(rbac_graph)
    with runtime_role(), patient_session_context(session):
        submit_intake(response_id=response.pk, answers=ANSWERS, expected_revision=1)
    username = User.objects.get(pk=rbac_graph.physician).username
    device = create_totp_device(rbac_graph.physician, confirmed=True)
    client = Client()
    url = f"/intake/clinics/{rbac_graph.clinic_a}/questionnaires/"
    with http_runtime_role(), fixed_otp_time():
        login = client.post(
            "/auth/login/", {"username": username, "password": RBAC_RAW_CREDENTIAL}
        )
        assert login.status_code == 302
        assert client.get(url).status_code == 302
        verified = client.post(
            "/auth/verify/",
            {"otp_device": device.persistent_id, "otp_token": token_for(device)},
        )
        assert verified.status_code == 302
        assert client.get(url).status_code == 200
        status = client.post(
            url, {"action": "status", "enrollment_id": str(response.enrollment_id)}
        )
        assert status.context["statuses"] == [(response.pk, "submitted", 2)]
        payload = {"response_id": str(response.pk), "revision": "2"}
        inspected = client.post(url, {**payload, "action": "inspect"})
        assert inspected.context["response"].answers == ANSWERS
        invalid = client.post(url, {**payload, "action": "reopen", "reason": ""})
        assert invalid.context["error"]
        reopened = client.post(
            url, {**payload, "action": "reopen", "reason": "Synthetic correction"}
        )
        assert reopened.context["response"].state == "draft"
        assert client.post(url, {**payload, "action": "invalid"}).status_code == 403
        assert (
            client.post(
                url, {"action": "inspect", "response_id": "invalid"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                url, {"action": "inspect", "response_id": str(uuid4())}
            ).status_code
            == 403
        )


def test_questionnaire_database_policy_and_resolver_catalog() -> None:
    tables = [
        "intake_questionnairetemplate",
        "intake_questionnaireresponse",
        "intake_questionnaireevent",
    ]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity, "
            "relowner::regrole::text FROM pg_class "
            "WHERE relnamespace = 'clinic_app'::regnamespace AND relname = ANY(%s)",
            [tables],
        )
        assert set(cursor.fetchall()) == {
            (t, True, True, "clinic_owner") for t in tables
        }
        cursor.execute(
            "SELECT tablename, policyname, roles, cmd FROM pg_policies "
            "WHERE schemaname = 'clinic_app' AND tablename = ANY(%s)",
            [tables],
        )
        policies = {(t, p, tuple(roles), cmd) for t, p, roles, cmd in cursor.fetchall()}
        assert policies == {
            (t, "setup_tenant", ("clinic_owner",), "ALL") for t in tables
        } | {
            (tables[0], "template_read", ("clinic_app",), "SELECT"),
            (tables[0], "template_write", ("clinic_app",), "INSERT"),
            (tables[1], "response_read", ("clinic_app",), "SELECT"),
            (tables[1], "response_assign", ("clinic_app",), "INSERT"),
            (tables[1], "response_write", ("clinic_app",), "UPDATE"),
            (tables[2], "event_read", ("clinic_app",), "SELECT"),
        }
        cursor.execute(
            "SELECT table_name, privilege_type "
            "FROM information_schema.role_table_grants "
            "WHERE grantee = 'clinic_app' AND table_schema = 'clinic_app' "
            "AND table_name = ANY(%s)",
            [tables],
        )
        assert set(cursor.fetchall()) == {(t, "SELECT") for t in tables} | {
            (tables[0], "INSERT"),
            (tables[1], "INSERT"),
        }
        cursor.execute(
            "SELECT table_name, column_name FROM information_schema.role_column_grants "
            "WHERE grantee = 'clinic_app' AND table_schema = 'clinic_app' "
            "AND privilege_type = 'UPDATE' AND table_name = ANY(%s)",
            [tables],
        )
        assert set(cursor.fetchall()) == {
            (tables[1], field)
            for field in (
                "answers",
                "state",
                "revision",
                "submitted_at",
                "reopen_reason",
                "updated_at",
            )
        }
        cursor.execute(
            "SELECT proname, proowner::regrole::text, prosecdef, proconfig, "
            "has_function_privilege('clinic_app', oid, 'EXECUTE'), "
            "EXISTS (SELECT 1 FROM aclexplode(proacl) acl "
            "WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE') "
            "FROM pg_proc WHERE pronamespace = 'clinic_app'::regnamespace "
            "AND proname LIKE 'questionnaire_%'",
        )
        functions = cursor.fetchall()
        assert {row[0] for row in functions} == {
            "questionnaire_staff",
            "questionnaire_patient_enrollment",
            "questionnaire_completion",
            "questionnaire_immutable",
            "questionnaire_response_guard",
            "questionnaire_receipt",
        }
        for name, owner, definer, config, executable, public in functions:
            assert owner == "clinic_resolver"
            assert definer is True
            assert config == ["search_path=pg_catalog, clinic_app, pg_temp"]
            assert public is False
            assert executable == (
                name
                in {
                    "questionnaire_staff",
                    "questionnaire_patient_enrollment",
                    "questionnaire_completion",
                }
            )


def test_boundary_lengths_false_and_optional_are_valid() -> None:
    questions = validate_questions(QUESTIONS)
    assert (
        validate_answers(questions, {**ANSWERS, "q_text": "x" * 40}, submitting=True)[
            "q_bool"
        ]
        is False
    )
    assert validate_answers(questions, {}, submitting=False) == {}
    with pytest.raises(ValidationError):
        validate_answers(questions, {"q_text": "x" * 41}, submitting=False)


def test_physician_assigns_from_one_appointment_once(rbac_graph: RbacGraph) -> None:
    """The agenda control resolves the enrollment server-side; retries converge."""
    graph = rbac_graph
    appointment, _ = encounter_seed(graph)
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        template = publish_template(
            clinic_id=graph.clinic_a,
            key="preconsulta",
            title="Pré-consulta sintética",
            questions=QUESTIONS,
        )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert [t.pk for t in published_templates(clinic_id=graph.clinic_a)] == [
            template.pk
        ]
        first, created = assign_for_appointment(
            clinic_id=graph.clinic_a,
            appointment_id=appointment.pk,
            template_id=template.pk,
        )
        again, repeated = assign_for_appointment(
            clinic_id=graph.clinic_a,
            appointment_id=appointment.pk,
            template_id=template.pk,
        )
        assert (created, repeated) == (True, False)
        assert again.pk == first.pk
        assert first.appointment_id == appointment.pk
        assert first.patient_id == appointment.patient_id
        with pytest.raises(PatientAccessDeniedError):
            assign_for_appointment(
                clinic_id=graph.clinic_a,
                appointment_id=uuid4(),
                template_id=template.pk,
            )
    with (
        runtime_role(),
        tenant_context(graph.shared_user, graph.organization_a),
        pytest.raises(CurrentActorError),
    ):
        assign_for_appointment(
            clinic_id=graph.clinic_a,
            appointment_id=appointment.pk,
            template_id=template.pk,
        )
