"""Template v2 preserves v1 bindings and supplies no clinical defaults."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.ehr.models import SpecialtyTemplate
from apps.ehr.services import (
    SOAP_FIELDS,
    create_draft,
    open_encounter,
    publish_template,
)
from apps.ehr.template_schema import validate_template
from apps.identity.current_context import CurrentActorError
from apps.tenancy.db import tenant_context
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction

from identity.permission_support import owner_context
from patient_service_support import runtime_role
from renewal.test_encounters import seed

if TYPE_CHECKING:
    from conftest import RbacGraph

FIELDS: list[dict[str, object]] = [
    {"type": "text", "max_length": 120},
    {"type": "number", "unit": "Synthetic unit", "range": {"min": -5, "max": 10}},
    {"type": "choice", "options": ["Synthetic A", "Synthetic B"]},
    {"type": "multichoice", "options": ["Synthetic A", "Synthetic B"]},
    {"type": "date"},
    {"type": "boolean"},
    {"type": "scale-ref", "scale_ref": "synthetic_scale", "scale_version": 1},
]


def definition() -> dict[str, object]:
    return {
        **dict.fromkeys(SOAP_FIELDS, ""),
        "schema_version": 2,
        "state": "published",
        "fields": [
            {
                "id": f"field_{index}",
                "section": "subjective",
                "label": "Synthetic field",
                "required": False,
                **field,
            }
            for index, field in enumerate(FIELDS)
        ],
        "phrases": [
            {
                "id": "instructions",
                "text": "Synthetic phrase",
                "scope": "clinic",
                "clinician_id": None,
            }
        ],
        "optional_sections": ["objective"],
    }


def test_all_field_types_validate_without_default_answers() -> None:
    validate_template(definition(), clinician_id=uuid4())


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("state", ["draft", "published", "retired"])
def test_template_states_append_registered_metadata_audit(
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
            prompts={**definition(), "state": state},
        )
    suffix = "draft_created" if state == "draft" else state
    with owner_context(rbac_graph.organization_a):
        event = AuditEvent.objects.get(
            event_type=f"ehr.template.{suffix}", affected_record_id=str(template.pk)
        )
        assert event.actor_user_id == rbac_graph.physician
        assert event.payload == {
            "clinic_id": str(rbac_graph.clinic_a),
            "object_verb": suffix,
        }


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema_version": 3},
        {"state": "active"},
        {"default": "normal"},
        {
            "fields": [
                {"id": "injected", "type": "expression", "expression": "normal()"}
            ]
        },
        {
            "fields": [
                {
                    "id": "x",
                    "section": "plan",
                    "label": "Synthetic",
                    "required": False,
                    "type": "number",
                    "unit": "u",
                    "range": {"min": 10, "max": 0},
                }
            ]
        },
        {
            "phrases": [
                {
                    "id": "x",
                    "text": "<script>normal()</script>",
                    "scope": "clinic",
                    "clinician_id": None,
                }
            ]
        },
        {"optional_sections": ["not_soap"]},
        {"subjective": "a" * 1001},
    ],
)
def test_malformed_v2_is_refused(mutation: dict[str, object]) -> None:
    candidate = {**definition(), **mutation}
    with pytest.raises(ValidationError):
        validate_template(candidate, clinician_id=uuid4())


def test_clinician_phrases_cannot_claim_another_author() -> None:
    actor = uuid4()
    candidate = definition()
    candidate["phrases"] = [
        {
            "id": "mine",
            "text": "Synthetic phrase",
            "scope": "clinician",
            "clinician_id": str(actor),
        }
    ]
    validate_template(candidate, clinician_id=actor)
    with pytest.raises(ValidationError):
        validate_template(candidate, clinician_id=uuid4())


@pytest.mark.django_db(transaction=True)
def test_v1_v2_publication_is_immutable_and_creates_empty_soap(
    rbac_graph: RbacGraph,
) -> None:
    appointment, legacy = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        template = publish_template(
            clinic_id=rbac_graph.clinic_a,
            key="typed",
            title="Synthetic",
            prompts=definition(),
        )
        assert template.prompts == definition()
        encounter = open_encounter(
            clinic_id=rbac_graph.clinic_a, appointment_id=appointment.pk
        )
        version = create_draft(
            clinic_id=rbac_graph.clinic_a,
            encounter_id=encounter.pk,
            template_id=template.pk,
        )
        version.refresh_from_db()
        assert all(getattr(version, field) == "" for field in SOAP_FIELDS)
        assert SpecialtyTemplate.objects.get(pk=legacy.pk).prompts == legacy.prompts
        with pytest.raises(DatabaseError), transaction.atomic():
            SpecialtyTemplate.objects.filter(pk=template.pk).update(title="Changed")
        with pytest.raises(DatabaseError), transaction.atomic():
            SpecialtyTemplate.objects.filter(pk=template.pk).delete()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("state", ["draft", "retired"])
def test_unpublished_template_cannot_bind_a_new_draft(
    rbac_graph: RbacGraph,
    state: str,
) -> None:
    appointment, _legacy = seed(rbac_graph)
    candidate = {**definition(), "state": state}
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        template = publish_template(
            clinic_id=rbac_graph.clinic_a,
            key="unpublished",
            title="Synthetic",
            prompts=candidate,
        )
        encounter = open_encounter(
            clinic_id=rbac_graph.clinic_a, appointment_id=appointment.pk
        )
        with pytest.raises(DatabaseError), transaction.atomic():
            create_draft(
                clinic_id=rbac_graph.clinic_a,
                encounter_id=encounter.pk,
                template_id=template.pk,
            )


@pytest.mark.django_db(transaction=True)
def test_legacy_trigger_still_requires_exactly_four_keys(rbac_graph: RbacGraph) -> None:
    seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        for prompts in (
            {"subjective": ""},
            {**dict.fromkeys(SOAP_FIELDS, ""), "extra": ""},
        ):
            with pytest.raises(DatabaseError), transaction.atomic():
                SpecialtyTemplate.objects.create(
                    organization_id=rbac_graph.organization_a,
                    clinic_id=rbac_graph.clinic_a,
                    key="invalid",
                    title="Synthetic",
                    version=1,
                    prompts=prompts,
                )


@pytest.mark.django_db(transaction=True)
def test_publication_denies_reception_and_cross_clinic(rbac_graph: RbacGraph) -> None:
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
        pytest.raises(CurrentActorError),
    ):
        publish_template(
            clinic_id=rbac_graph.clinic_a,
            key="denied",
            title="Synthetic",
            prompts=definition(),
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
        pytest.raises(CurrentActorError),
    ):
        publish_template(
            clinic_id=rbac_graph.clinic_c,
            key="denied",
            title="Synthetic",
            prompts=deepcopy(definition()),
        )
