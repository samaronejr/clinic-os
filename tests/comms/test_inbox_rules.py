"""Window, purpose-separation and delivery-state rules without a database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from apps.comms.inbox_rules import (
    SERVICE_WINDOW,
    SendDecision,
    SendRequest,
    advance_provider_state,
    channel_allows_purpose,
    decide_send,
    effective_delivery_state,
    placeholder_count,
    render_template,
    template_fits_purpose,
    template_transition_allowed,
    window_closes_at,
    window_open,
)

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def _decide(**overrides: object) -> SendDecision:
    terms: dict[str, object] = {
        "channel": "whatsapp",
        "purpose": "transactional",
        "conversation_state": "open",
        "now": NOW,
        "last_inbound_at": NOW - timedelta(hours=1),
        "template_category": None,
        "template_status": None,
        "template_channel": None,
        "linked_to_patient": True,
    }
    terms.update(overrides)
    return decide_send(SendRequest(**terms))  # type: ignore[arg-type]


def test_window_is_exclusive_at_twenty_four_hours() -> None:
    inbound = NOW - SERVICE_WINDOW
    assert not window_open(last_inbound_at=inbound, now=NOW)
    assert window_open(last_inbound_at=inbound + timedelta(seconds=1), now=NOW)
    assert window_closes_at(last_inbound_at=inbound) == NOW
    assert not window_open(last_inbound_at=None, now=NOW)
    assert not window_open(last_inbound_at=NOW + timedelta(seconds=1), now=NOW)


def test_free_text_after_window_requires_an_approved_template() -> None:
    expired = _decide(last_inbound_at=NOW - SERVICE_WINDOW)
    assert (expired.allowed, expired.reason) == (
        False,
        "window_expired_template_required",
    )
    never = _decide(last_inbound_at=None)
    assert never.reason == "window_expired_template_required"
    template = _decide(
        last_inbound_at=NOW - SERVICE_WINDOW,
        template_category="utility",
        template_status="approved",
        template_channel="whatsapp",
    )
    assert (template.allowed, template.meter_category) == (True, "utility")


@pytest.mark.parametrize(
    "status", ["draft", "pending", "rejected", "paused", "disabled"]
)
def test_only_an_approved_template_may_be_sent(status: str) -> None:
    decision = _decide(
        template_category="utility", template_status=status, template_channel="whatsapp"
    )
    assert (decision.allowed, decision.reason) == (False, "template_not_approved")


def test_template_must_match_channel_and_purpose() -> None:
    wrong_channel = _decide(
        template_category="utility", template_status="approved", template_channel="sms"
    )
    assert wrong_channel.reason == "template_channel_mismatch"
    marketing_in_care = _decide(
        purpose="care",
        template_category="marketing",
        template_status="approved",
        template_channel="whatsapp",
    )
    assert marketing_in_care.reason == "template_purpose_mismatch"
    utility_in_marketing = _decide(
        purpose="marketing",
        template_category="utility",
        template_status="approved",
        template_channel="whatsapp",
    )
    assert utility_in_marketing.reason == "template_purpose_mismatch"


def test_marketing_is_template_only_and_never_in_app() -> None:
    assert _decide(purpose="marketing").reason == "marketing_requires_template"
    assert not channel_allows_purpose(channel="portal", purpose="marketing")
    assert (
        _decide(channel="portal", purpose="marketing").reason
        == "channel_purpose_mismatch"
    )
    assert template_fits_purpose(category="marketing", purpose="marketing")
    assert not template_fits_purpose(category="authentication", purpose="care")
    assert not template_fits_purpose(category="bogus", purpose="care")


def test_care_content_needs_a_verified_patient_link() -> None:
    unlinked = _decide(purpose="care", linked_to_patient=False)
    assert (unlinked.allowed, unlinked.reason) == (False, "patient_not_verified")
    assert _decide(purpose="care").allowed


def test_closed_conversation_and_unknown_scope_refuse() -> None:
    assert _decide(conversation_state="closed").reason == "conversation_closed"
    assert _decide(channel="fax").reason == "invalid_scope"
    assert _decide(purpose="chat").reason == "invalid_scope"


def test_windowless_channels_never_need_a_window() -> None:
    for channel in ("email", "sms", "portal"):
        assert _decide(channel=channel, last_inbound_at=None).allowed


@pytest.mark.parametrize(
    ("operation", "provider", "expected"),
    [
        ("pending", "", "queued"),
        ("in_progress", "", "queued"),
        ("succeeded", "", "sent"),
        ("succeeded", "delivered", "delivered"),
        ("succeeded", "read", "read"),
        ("delivered", "sent", "delivered"),
        ("failed", "", "failed"),
        ("cancelled", "", "failed"),
        ("succeeded", "failed", "failed"),
    ],
)
def test_effective_state_combines_outbox_and_provider(
    operation: str, provider: str, expected: str
) -> None:
    assert (
        effective_delivery_state(operation_status=operation, provider_state=provider)
        == expected
    )


def test_provider_state_is_monotonic_and_keeps_delivery_proof() -> None:
    assert advance_provider_state(current="", incoming="sent") == "sent"
    assert advance_provider_state(current="read", incoming="delivered") == "read"
    assert advance_provider_state(current="delivered", incoming="failed") == "delivered"
    assert advance_provider_state(current="failed", incoming="delivered") == "delivered"
    assert advance_provider_state(current="sent", incoming="failed") == "failed"
    assert advance_provider_state(current="sent", incoming="unknown") == "sent"


def test_template_rendering_is_strict_about_arity_and_content() -> None:
    body = "Olá {{1}}, sua consulta é às {{2}}."
    assert placeholder_count(body) == 2
    assert render_template(body=body, parameters=("Ana", "10:00")) == (
        "Olá Ana, sua consulta é às 10:00."
    )
    assert render_template(body=body, parameters=("Ana",)) is None
    assert render_template(body=body, parameters=("Ana", "10\n11:00")) is None
    assert render_template(body=body, parameters=("Ana", " ")) is None
    assert render_template(body=body, parameters=("Ana", "x" * 257)) is None
    assert placeholder_count("sem parâmetros") == 0
    assert placeholder_count("{{2}} sem o primeiro") is None


def test_template_approval_transitions_mirror_the_database_guard() -> None:
    allowed = [
        ("draft", "pending"),
        ("pending", "approved"),
        ("pending", "rejected"),
        ("approved", "paused"),
        ("paused", "approved"),
        ("approved", "disabled"),
    ]
    assert all(template_transition_allowed(current=a, new=b) for a, b in allowed)
    for current, new in [
        ("draft", "approved"),
        ("rejected", "approved"),
        ("disabled", "approved"),
        ("approved", "pending"),
        ("approved", "bogus"),
    ]:
        assert not template_transition_allowed(current=current, new=new)
