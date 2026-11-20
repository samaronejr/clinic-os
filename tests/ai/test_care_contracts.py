"""Only facts from the exact, current synthetic plan support a proposal."""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID

import pytest
from apps.ai.care_contracts import CareRequest, synthetic_reply, validate_reply
from apps.ai.contracts import GatewayInputError, ProviderResult, Usage
from apps.ai.output_validation import _validate_structured_output

from ai.care_support import BUNDLE_PLAN, MEDICATION_PLAN
from ai.test_units import request

SOURCE = CareRequest(UUID(int=1), 1, UUID(int=2), BUNDLE_PLAN)


def test_windows_belong_to_the_requested_kind_not_anywhere_in_the_plan() -> None:
    reply = json.loads(synthetic_reply(SOURCE.encode()))
    window, staff, questionnaire = reply["items"]
    reply["items"] = [
        {**window, "start_days": 14, "end_days": 21},
        {**questionnaire, "start_days": 42, "end_days": 49},
        staff,
        window,
        window,
    ]
    accepted, refused = validate_reply(json.dumps(reply), SOURCE)
    assert [(item.position, item.kind) for item in accepted] == [
        (2, "staff_coordination"),
        (3, "return_window"),
    ]
    assert [(item.position, item.reason) for item in refused] == [
        (0, "window_invalid"),
        (1, "window_invalid"),
        (4, "duplicate"),
    ]


@pytest.mark.parametrize(
    "text",
    [
        MEDICATION_PLAN + " Retorno em 2 semanas.",
        "Não realizar: " + BUNDLE_PLAN,
        BUNDLE_PLAN.replace("6 semanas", "6\u00a0semanas"),
        " ",
    ],
)
def test_unknown_complete_plan_cannot_borrow_a_supported_phrase(text: str) -> None:
    accepted, refused = validate_reply(
        synthetic_reply(SOURCE.encode()), replace(SOURCE, text=text)
    )
    assert not accepted
    assert [item.reason for item in refused] == ["kind_forbidden"] * 3


def test_gateway_requires_a_valid_care_source_even_with_a_well_formed_reply() -> None:
    with pytest.raises(GatewayInputError):
        _validate_structured_output(
            request=replace(request(), content=replace(SOURCE, text="").encode()),
            result=ProviderResult(synthetic_reply(SOURCE.encode()), Usage()),
        )
