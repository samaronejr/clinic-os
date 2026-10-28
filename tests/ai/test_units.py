"""Database-free arithmetic, egress, schema, and provider contract proofs."""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from typing import TYPE_CHECKING

import pytest
from apps.ai.adapters import RecordedFixtureProvider, SyntheticProvider
from apps.ai.contracts import (
    Budget,
    GatewayInputError,
    Price,
    ProviderRequest,
    ProviderResult,
    ProviderUnavailableError,
    Purpose,
    Usage,
)
from apps.ai.forms import PolicyForm
from apps.ai.output_validation import call_provider, validate_result
from apps.ai.policy import OrganizationPolicy, parse_policy
from apps.ai.prompt_registry import load_prompt
from django.db import connection
from django.utils.translation import override

if TYPE_CHECKING:
    from apps.ai.contracts import Method
    from apps.ai.policy import JsonValue


def request() -> ProviderRequest:
    return ProviderRequest(
        "fixture-v1",
        "https://ai.synthetic.invalid/v1/inference",
        "synthetic template",
        "SINTETICO-SENTINELA-INPUT",
        Usage(tokens_in=100, tokens_out=100, audio_seconds=60),
        30,
    )


def test_prices_count_cached_tokens_once_and_reserve_the_worst_mix() -> None:
    # Given: deliberately different input, output, cache, and audio prices.
    price = Price(31, 17, 5, 113)
    usage = Usage(tokens_in=13, tokens_out=7, tokens_cached=3, audio_seconds=2)
    # When: exact cost and the reservation are calculated.
    cost, reserved = price.cost(usage), price.reservation(usage)
    # Then: no token is double-counted and no fraction is rounded away.
    assert cost == 670
    assert reserved == 748


@pytest.mark.parametrize(
    "payload",
    [
        '{"tokens_in":true}',
        '{"tokens_out":1.5}',
        '{"audio_seconds":-1}',
        '{"tokens_in":1,"tokens_cached":2}',
        '{"tokens_in":1000001}',
    ],
)
def test_invalid_usage_cannot_be_constructed(payload: str) -> None:
    # Given: unit values outside the integer contract.
    data = json.loads(payload)
    # When/Then: the provider value object refuses at construction.
    with pytest.raises(GatewayInputError):
        Usage(**data)


@pytest.mark.parametrize("value", [-1, True, 10**15 + 1])
def test_a_call_cannot_supply_an_invalid_spending_ceiling(value: int) -> None:
    # Given/When/Then: malformed caller caps never enter admission arithmetic.
    with pytest.raises(GatewayInputError):
        Budget(Usage(), value)


@pytest.mark.parametrize("method", ["complete", "stream", "transcribe", "extract"])
@pytest.mark.parametrize("recorded", [False, True])
def test_all_provider_methods_are_deterministic_and_content_free_in_repr(
    method: Method,
    recorded: bool,
) -> None:
    # Given: a real offline adapter and sentinel-bearing input.
    provider = RecordedFixtureProvider() if recorded else SyntheticProvider()
    query = request()
    if recorded:
        query = replace(
            query, endpoint="https://recorded.synthetic.invalid/v1/inference"
        )
    # When: each public protocol operation executes.
    result = call_provider(provider, request=query, method=method)
    # Then: the adapter is deterministic and never echoes the input.
    assert result == call_provider(provider, request=query, method=method)
    assert query.content not in result.content
    assert query.content not in repr(query)
    assert result.content not in repr(result)
    assert provider.real_enabled is False


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://unapproved.invalid/v1/inference",
        "https://ai.synthetic.invalid.attacker.invalid/v1/inference",
        "https://ai.synthetic.invalid@attacker.invalid/v1/inference",
        "http://ai.synthetic.invalid/v1/inference",
        "https://ai.synthetic.invalid:443/v1/inference",
        "https://ai.synthetic.invalid/v1/inference?redirect=elsewhere",
        "https://[invalid",
    ],
)
def test_egress_refuses_every_unapproved_destination(endpoint: str) -> None:
    # Given: an endpoint outside the adapter's exact allowlist.
    query = replace(request(), endpoint=endpoint)
    # When/Then: refusal happens before any inference operation.
    with pytest.raises(ProviderUnavailableError):
        SyntheticProvider().complete(request=query)


def test_transaction_guard_refuses_without_opening_a_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a simulated request transaction, without a database connection.
    monkeypatch.setattr(connection, "in_atomic_block", True)
    # When/Then: even the offline adapter refuses the transaction-bound call.
    with pytest.raises(ProviderUnavailableError):
        SyntheticProvider().complete(request=request())


@pytest.mark.parametrize(
    "content",
    [
        '{"text":"one","text":"two"}',
        '{"text":"draft","tool":"refund"}',
        '{"text":null}',
        '{"text":{"command":"execute"}}',
        '["draft"]',
    ],
)
def test_extraction_output_is_a_closed_schema(content: str) -> None:
    # Given: untrusted structured output.
    response = ProviderResult(content, Usage())
    # When/Then: extra keys, duplicate keys, and non-text values all refuse.
    with pytest.raises(GatewayInputError):
        validate_result(response, limits=Usage(), method="extract")


def test_recorded_fixture_never_accepts_an_arbitrary_recording() -> None:
    # Given: the reviewed recording's exact endpoint.
    query = replace(
        request(), endpoint="https://recorded.synthetic.invalid/v1/inference"
    )
    provider = RecordedFixtureProvider()
    # When/Then: the known recording works; a path-shaped model id does not.
    assert provider.complete(request=query).usage == Usage(tokens_in=3, tokens_out=5)
    with pytest.raises(ProviderUnavailableError):
        provider.complete(request=replace(query, model_id="../../private"))


@pytest.mark.parametrize("purpose", list(Purpose))
def test_prompt_provenance_hashes_the_exact_versioned_bytes(purpose: Purpose) -> None:
    # Given/When: the code registry resolves a purpose, not a caller path.
    prompt, digest = load_prompt(purpose)
    # Then: the machine-consumed digest identifies the exact template bytes.
    assert digest == sha256(prompt.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    "raw",
    [
        {"enabled": True},
        {
            "enabled": True,
            "purposes": ["brief"],
            "monthly_budget_micros": True,
            "purpose_budgets": {"brief": 1},
        },
        {
            "enabled": True,
            "purposes": ["unknown"],
            "monthly_budget_micros": 1,
            "purpose_budgets": {"unknown": 1},
        },
        {
            "enabled": True,
            "purposes": ["brief", "brief"],
            "monthly_budget_micros": 1,
            "purpose_budgets": {"brief": 1},
        },
    ],
)
def test_policy_refuses_open_fields_and_ambiguous_money(raw: JsonValue) -> None:
    # Given/When/Then: raw JSON never reaches the typed policy unchecked.
    with pytest.raises(GatewayInputError):
        parse_policy(raw)


def test_native_form_retains_exact_integer_limits() -> None:
    # Given: a complete native submission, with distinct purpose and org caps.
    with override("en"):
        form = PolicyForm(
            {
                "expected_version": "1",
                "enabled": "on",
                "purposes": ["brief"],
                "monthly_budget_micros": "1000031",
                "limit_brief": "300017",
            }
        )
        # When: Django parses the submitted field values.
        assert form.is_valid(), form.errors
        policy = form.policy()
    # Then: no float conversion or hidden model/region field is involved.
    assert policy.monthly_budget_micros == 1_000_031
    assert dict(policy.purpose_budgets)["brief"] == 300_017
    assert "region" not in form.fields
    assert parse_policy(policy.as_json()) == OrganizationPolicy(
        enabled=True,
        purposes=("brief",),
        monthly_budget_micros=1_000_031,
        purpose_budgets=policy.purpose_budgets,
    )
