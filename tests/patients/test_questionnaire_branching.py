"""Declarative branching parity corpus and real patient-session provenance."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.identity.clinic_settings_forms import QuestionnaireForm
from apps.intake.access import PatientAccessDeniedError
from apps.intake.branching import validate_definition, visible_questions
from apps.intake.models import QuestionnaireEvent, QuestionnaireTemplate
from apps.intake.patient_access import patient_session_context
from apps.intake.questionnaires import (
    answer_values,
    assign_questionnaire,
    carry_forward_response,
    publish_template,
    save_response,
    unverified_answers,
    validate_answers,
)
from apps.tenancy.db import tenant_context
from django.core.exceptions import ValidationError

from identity.permission_support import owner_context
from otp_test_support import runtime_role as http_runtime_role
from patient_service_support import runtime_role
from renewal.test_questionnaires import _patient_client, seed

if TYPE_CHECKING:
    from conftest import RbacGraph

VECTORS = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures/questionnaire-branching-vectors.json"
    ).read_text()
)
DEFINITION = VECTORS["definition"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("state", ["draft", "published", "retired"])
def test_questionnaire_states_append_registered_metadata_audit(
    rbac_graph: RbacGraph, state: str
) -> None:
    seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        template = publish_template(
            clinic_id=rbac_graph.clinic_a,
            key="audit_state",
            title="Synthetic",
            questions={**DEFINITION, "state": state},
        )
    suffix = "draft_created" if state == "draft" else state
    with owner_context(rbac_graph.organization_a):
        event = AuditEvent.objects.get(
            event_type=f"intake.questionnaire_template.{suffix}",
            affected_record_id=str(template.pk),
        )
        assert event.actor_user_id == rbac_graph.physician
        assert event.payload == {
            "clinic_id": str(rbac_graph.clinic_a),
            "object_verb": suffix,
        }


class BranchCase(TypedDict):
    name: str
    answers: dict[str, object]
    visible: list[str]


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda case: case["name"])
def test_shared_python_vectors(case: BranchCase) -> None:
    validate_definition(DEFINITION)
    assert visible_questions(DEFINITION, case["answers"]) == case["visible"]


@pytest.mark.parametrize(
    "rule",
    [
        {
            "when": {"question": "q_bool", "op": "eval", "value": True},
            "show": ["q_detail"],
        },
        {
            "when": {
                "question": "q_bool",
                "op": "eq",
                "value": {"expression": "normal()"},
            },
            "show": ["q_detail"],
        },
        {
            "when": {"question": "q_detail", "op": "eq", "value": "{{normal()}}"},
            "show": ["q_nested"],
        },
        {
            "when": {"question": "q_missing", "op": "eq", "value": True},
            "show": ["q_detail"],
        },
        {
            "when": {"question": "q_detail", "op": "eq", "value": "literal"},
            "show": ["q_bool"],
        },
        {
            "when": {"question": "q_bool", "op": "gt", "value": True},
            "show": ["q_detail"],
        },
        {
            "when": {"question": "q_number", "op": "eq", "value": True},
            "show": ["q_high"],
        },
        {
            "when": {"question": "q_bool", "op": "eq", "value": True},
            "show": ["q_detail"],
            "expression": "true",
        },
        {
            "when": {"question": "q_choice", "op": "in", "value": [["A"]]},
            "show": ["q_in"],
        },
        {"when": {"question": "q_bool", "op": "eq", "value": True}, "show": ["q_bool"]},
    ],
)
def test_malformed_or_executable_rule_is_rejected(rule: dict[str, object]) -> None:
    candidate = {**DEFINITION, "rules": [rule]}
    with pytest.raises(ValidationError):
        validate_definition(candidate)


def test_hidden_values_are_removed_and_required_checks_follow_visibility() -> None:
    assert validate_answers(
        DEFINITION,
        {"q_bool": False, "q_detail": "literal", "q_nested": "injected"},
        submitting=True,
    ) == {"q_bool": False}
    with pytest.raises(ValidationError):
        validate_answers(DEFINITION, {"q_bool": True}, submitting=True)
    assert validate_answers(
        DEFINITION, {"q_bool": True, "q_detail": "Reviewed"}, submitting=True
    ) == {"q_bool": True, "q_detail": "Reviewed"}


def test_blank_source_never_requires_a_hidden_follow_up() -> None:
    candidate = deepcopy(DEFINITION)
    candidate["questions"][1]["required"] = False
    candidate["questions"][5]["required"] = True
    assert validate_answers(
        candidate, {"q_bool": True, "q_detail": ""}, submitting=True
    ) == {"q_bool": True}


@pytest.mark.parametrize("value", [True, "6", float("nan"), float("inf"), 10**1000])
def test_numeric_answers_do_not_coerce_or_overflow(value: object) -> None:
    with pytest.raises(ValidationError):
        validate_answers(DEFINITION, {"q_number": value}, submitting=False)


def test_fixed_authoring_rows_create_typed_declarative_rule() -> None:
    form = QuestionnaireForm(
        {
            "key": "synthetic",
            "title": "Synthetic",
            "q1_label": "Synthetic question",
            "q1_type": "boolean",
            "q2_label": "Synthetic follow-up",
            "q2_type": "text",
            "q2_when_question": "1",
            "q2_when_op": "eq",
            "q2_when_value": "Sim",
        }
    )
    assert form.is_valid(), form.errors
    result = form.cleaned_data["questions"]
    validate_definition(result)
    assert result["rules"] == [
        {"when": {"question": "q_1", "op": "eq", "value": True}, "show": ["q_2"]}
    ]


@pytest.mark.django_db(transaction=True)
def test_carry_forward_requires_explicit_review_and_receipts_retain_provenance(
    rbac_graph: RbacGraph,
) -> None:
    legacy, session = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        template = publish_template(
            clinic_id=rbac_graph.clinic_a,
            key="branching",
            title="Synthetic",
            questions=deepcopy(DEFINITION),
        )
        source = assign_questionnaire(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=legacy.enrollment_id,
            template_id=template.pk,
        )
        target = assign_questionnaire(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=legacy.enrollment_id,
            template_id=template.pk,
        )
    with runtime_role(), patient_session_context(session):
        save_response(
            response_id=source.pk,
            answers={"values": {"q_bool": False}, "confirmed": []},
            expected_revision=1,
            submit=True,
        )
        carried = carry_forward_response(
            clinic_id=rbac_graph.clinic_a,
            response_id=target.pk,
            source_response_id=source.pk,
            expected_revision=1,
        )
        assert answer_values(carried) == {"q_bool": False}
        assert unverified_answers(carried) == ["q_bool"]
    client = _patient_client(session)
    with http_runtime_role():
        stale = client.post(
            "/patient/questionnaires/",
            {
                "action": "carry",
                "response_id": str(target.pk),
                "source_response_id": str(source.pk),
                "revision": "1",
            },
        )
        assert stale.status_code == 200
        assert stale.context["response"].revision == 2
        assert stale.context["form"].initial["q_bool"] is False
        assert b"data-carry-refused" in stale.content
    with runtime_role(), patient_session_context(session):
        with pytest.raises(PatientAccessDeniedError):
            carry_forward_response(
                clinic_id=rbac_graph.clinic_c,
                response_id=target.pk,
                source_response_id=source.pk,
                expected_revision=2,
            )
        with pytest.raises(PatientAccessDeniedError):
            carry_forward_response(
                clinic_id=rbac_graph.clinic_a,
                response_id=target.pk,
                source_response_id=uuid4(),
                expected_revision=2,
            )
        with pytest.raises(ValidationError):
            save_response(
                response_id=target.pk,
                expected_revision=2,
                submit=True,
                answers={"values": {"q_bool": False}, "confirmed": ["q_missing"]},
            )
        with pytest.raises(ValidationError):
            save_response(
                response_id=target.pk,
                expected_revision=2,
                submit=True,
                answers={"values": {"q_bool": False}, "confirmed": []},
            )
        saved = save_response(
            response_id=target.pk,
            expected_revision=2,
            submit=True,
            answers={"values": {"q_bool": False}, "confirmed": ["q_bool"]},
        )
        assert saved.state == "submitted"
        assert unverified_answers(saved) == []
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        receipts = list(
            QuestionnaireEvent.objects.filter(response_id=target.pk).order_by(
                "revision"
            )
        )
        assert receipts[1].answers == {
            "values": {"q_bool": False},
            "unverified": ["q_bool"],
        }
        assert receipts[2].answers == {"values": {"q_bool": False}, "unverified": []}


@pytest.mark.django_db(transaction=True)
def test_malformed_rule_is_rejected_at_publish_without_creating_a_version(
    rbac_graph: RbacGraph,
) -> None:
    seed(rbac_graph)
    invalid = {
        **DEFINITION,
        "rules": [
            {
                "when": {
                    "question": "q_bool",
                    "op": "eq",
                    "value": {"expression": "true"},
                },
                "show": ["q_detail"],
            }
        ],
    }
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        with pytest.raises(ValidationError):
            publish_template(
                clinic_id=rbac_graph.clinic_a,
                key="bad",
                title="Synthetic",
                questions=invalid,
            )
        assert not QuestionnaireTemplate.objects.filter(key="bad").exists()
