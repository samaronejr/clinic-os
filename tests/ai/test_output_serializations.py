"""B1: request formatting must never choose whether safety validation runs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from apps.ai.adapters import SyntheticProvider
from apps.ai.brief_contracts import ChartRequest, synthetic_reply, validate_reply
from apps.ai.contracts import (
    GatewayInputError,
    ProviderRequest,
    ProviderResult,
    Usage,
)
from apps.ai.decision_contracts import (
    DecisionRequest,
    parse_decision_request,
    synthetic_decision_reply,
    validate_decision_reply,
)
from apps.ai.output_validation import call_provider
from apps.ai.reference_catalog import load_catalog
from apps.core.chart_sources import ChartSource

from ai.serialization_support import SERIALIZATIONS, serialize


@pytest.fixture
def turn() -> DecisionRequest:
    source = ChartSource(
        "observations",
        "observation",
        UUID(int=3),
        1,
        datetime(2026, 10, 28, tzinfo=UTC),
        "Synthetic serialization boundary fact",
    )
    return DecisionRequest(
        UUID(int=1),
        ChartRequest(UUID(int=2), "1" * 64, "brief", "", (source.reference(),)),
        tuple(entry.reference() for entry in load_catalog()),
    )


def query(content: str) -> ProviderRequest:
    return ProviderRequest(
        "synthetic-v1",
        "https://ai.synthetic.invalid/v1/inference",
        "Synthetic boundary probe",
        content,
        Usage(tokens_in=100, tokens_out=100),
        30,
    )


@pytest.mark.parametrize("request_form", SERIALIZATIONS)
@pytest.mark.parametrize("output_form", SERIALIZATIONS)
@pytest.mark.parametrize("attack", ["valid", "diagnosis", "top_level", "citation"])
def test_decision_provider_boundary_is_serialization_invariant(
    turn: DecisionRequest,
    request_form: str,
    output_form: str,
    attack: str,
) -> None:
    content = serialize(turn.encode(), request_form)
    assert parse_decision_request(content) == turn
    data = json.loads(synthetic_decision_reply(turn.encode()))
    if attack == "diagnosis":
        data["model_hypotheses"][0]["diagnosis"] = "Synthetic forbidden diagnosis"
    elif attack == "top_level":
        data["diagnosis"] = "Synthetic forbidden diagnosis"
    elif attack == "citation":
        data["model_hypotheses"][0]["reference"]["key"] = "fabricated-citation"
    output = serialize(json.dumps(data), output_form)
    assert json.loads(output) == data

    class UntrustedProvider(SyntheticProvider):
        def complete(self, *, request: ProviderRequest) -> ProviderResult:
            return ProviderResult(output, Usage(tokens_in=1, tokens_out=1))

    if attack == "valid":
        result = call_provider(
            UntrustedProvider(), request=query(content), method="complete"
        )
        assert validate_decision_reply(result.content, turn)
    else:
        with pytest.raises(GatewayInputError):
            call_provider(
                UntrustedProvider(), request=query(content), method="complete"
            )


@pytest.mark.parametrize("request_form", SERIALIZATIONS)
@pytest.mark.parametrize("output_form", SERIALIZATIONS)
@pytest.mark.parametrize("valid", [False, True])
def test_chart_provider_boundary_is_serialization_invariant(
    turn: DecisionRequest,
    request_form: str,
    output_form: str,
    valid: bool,
) -> None:
    content = serialize(turn.chart.encode(), request_form)
    data = json.loads(synthetic_reply(turn.chart.encode()))
    if not valid:
        data["citations"][0]["id"] = str(UUID(int=99))
    output = serialize(json.dumps(data), output_form)

    class UntrustedProvider(SyntheticProvider):
        def complete(self, *, request: ProviderRequest) -> ProviderResult:
            return ProviderResult(output, Usage(tokens_in=1, tokens_out=1))

    if valid:
        result = call_provider(
            UntrustedProvider(), request=query(content), method="complete"
        )
        assert validate_reply(result.content, turn.chart)
    else:
        with pytest.raises(GatewayInputError):
            call_provider(
                UntrustedProvider(), request=query(content), method="complete"
            )


@pytest.mark.parametrize("form", SERIALIZATIONS)
@pytest.mark.parametrize("decision", [False, True])
def test_synthetic_adapter_selects_only_the_structural_root_schema(
    turn: DecisionRequest,
    form: str,
    decision: bool,
) -> None:
    content = serialize(turn.encode() if decision else turn.chart.encode(), form)
    result = call_provider(
        SyntheticProvider(), request=query(content), method="complete"
    )
    if decision:
        assert validate_decision_reply(result.content, turn)
    else:
        assert validate_reply(result.content, turn.chart)


@pytest.mark.parametrize(
    "content",
    [
        "Synthetic plain-text input",
        "{not JSON",
        "[]",
        "null",
        '{"nested":{"schema":"decision-support-request-v1"}}',
    ],
)
def test_generic_input_does_not_inherit_a_nested_or_incidental_schema(
    content: str,
) -> None:
    result = call_provider(
        SyntheticProvider(), request=query(content), method="complete"
    )
    assert result.content == "Synthetic draft. Human review required."
