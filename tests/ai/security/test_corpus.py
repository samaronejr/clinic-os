"""The machine-consumed fixture contract is bilingual and structurally complete."""

from __future__ import annotations

import base64
import json
from urllib.parse import unquote

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
    assert ("capability__in", CAPABILITIES) in constraint.condition.children
    for capability in CAPABILITIES:
        assert (
            PolicyTerms(capability, "A1", ("workflows.review_task",)).capability
            == capability
        )
    assert set(OPERATIONS) == {"workflows.review_task", "comms.synthetic_reminder"}
    with pytest.raises(ActionInputError):
        PolicyTerms("undeclared-capability", "A1", ("workflows.review_task",))
