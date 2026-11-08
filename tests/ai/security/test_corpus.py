"""The machine-consumed fixture contract is bilingual and structurally complete."""

from __future__ import annotations

import base64
import json
from urllib.parse import unquote
from uuid import uuid4

import pytest
from apps.ai.action_contracts import OPERATIONS, ActionInputError
from apps.ai.agent_policies import PolicyTerms
from apps.ai.models import AgentPolicyVersion
from django.db.models import CheckConstraint, Q

from ai.security.corpus import CASES, VARIANTS, AttackCase, Variant
from ai.security.support import CAPABILITIES


def test_corpus_covers_each_source_in_both_languages() -> None:
    assert {(case.surface, case.language) for case in CASES} == {
        (surface, language)
        for surface in ("transcript", "pdf", "email", "portal_message", "kb_page")
        for language in ("en", "pt-BR")
    }
    assert any("https://exfil.synthetic.invalid/" in case.content for case in CASES)
    assert any("<tool_call>" in case.content for case in CASES)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.identifier)
@pytest.mark.parametrize("variant", VARIANTS)
def test_nested_and_encoded_sources_preserve_the_attack_as_data(
    case: AttackCase, variant: Variant
) -> None:
    source = case.source(variant)
    assert source.model_data()["untrusted"] is True
    assert source.text not in repr(source)
    match variant:
        case "plain":
            decoded = source.text
        case "nested":
            decoded = json.loads(source.text)["quoted_document"]["messages"][0][
                "content"
            ]
        case "base64":
            decoded = base64.b64decode(source.text, validate=True).decode()
            assert not source.flagged
        case "url_encoded":
            decoded = unquote(source.text)
    assert decoded == case.content


def test_capability_matrix_cannot_silently_omit_a_new_registered_capability() -> None:
    constraint = next(
        item
        for item in AgentPolicyVersion._meta.constraints
        if item.name == "ai_agent_policy_values"
    )
    assert isinstance(constraint, CheckConstraint)
    assert isinstance(constraint.condition, Q)
    # Task 53: care coordination has no generic tool. Its one A1 command,
    # care_action.create, is attacked by tests/ai/test_care_coordination.py and
    # tests/ai/test_care_views.py (forgery, injection, staff vs clinician).
    # Task 60: revenue_cycle has two A1-only drafting commands, attacked by
    # tests/ai/test_revenue_cycle.py and test_revenue_views.py (fabrication,
    # upcoding, payer-text injection, cross-scope retrieval, approval bypass).
    assert (
        "capability__in",
        (*CAPABILITIES, "care_coordination", "revenue_cycle"),
    ) in constraint.condition.children
    for capability in CAPABILITIES:
        assert (
            PolicyTerms(capability, "A1", ("workflows.review_task",)).capability
            == capability
        )
    assert (
        PolicyTerms("care_coordination", "A1", ("care_action.create",)).capability
        == "care_coordination"
    )
    assert (
        PolicyTerms(
            "revenue_cycle", "A1", ("appeal.draft", "discrepancy.explain")
        ).capability
        == "revenue_cycle"
    )
    assert set(OPERATIONS) == {
        "workflows.review_task",
        "comms.synthetic_reminder",
        "care_action.create",
        "appeal.draft",
        "discrepancy.explain",
    }
    for capability, tools in (
        ("undeclared-capability", ("workflows.review_task",)),
        ("care_coordination", ("workflows.review_task",)),
        ("front_desk", ("care_action.create",)),
        ("revenue_cycle", ("workflows.review_task",)),
        ("revenue_cycle", ("care_action.create",)),
        ("front_desk", ("appeal.draft",)),
    ):
        with pytest.raises(ActionInputError):
            PolicyTerms(capability, "A1", tools)
    with pytest.raises(ActionInputError):
        PolicyTerms("revenue_cycle", "A3", ("appeal.draft",), principal_id=uuid4())
