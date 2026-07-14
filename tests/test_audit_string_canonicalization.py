from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from apps.audit.services import (
    AuditEventInput,
    AuditEventValueRejectedError,
    AuditPayloadValueRejectedError,
    AuditTrustedContext,
    _content_hash,
    _normalize_payload,
)


class _DynamicText(str):
    __slots__ = ()

    encode_calls = 0

    def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:
        type(self).encode_calls += 1
        if encoding == "utf-16be":
            return b"\xff\xff"
        return super().encode(encoding, errors)

    def strip(self, chars: str | None = None) -> str:
        return "spoofed-valid-text"


def _event(**overrides: str) -> AuditEventInput:
    fields = {
        "event_type": "audit.synthetic",
        "component_id": "test-suite",
        "affected_record_type": "record",
        "affected_record_id": "record-1",
    }
    fields.update(overrides)
    return AuditEventInput(
        event_type=fields["event_type"],
        component_id=fields["component_id"],
        component_ip=None,
        affected_record_type=fields["affected_record_type"],
        affected_record_id=fields["affected_record_id"],
        occurred_at_utc=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
    )


def test_payload_text_subtypes_are_frozen_before_canonical_sorting() -> None:
    dynamic_key = _DynamicText("http_method")
    dynamic_value = _DynamicText("GET")
    _DynamicText.encode_calls = 0

    normalized = _normalize_payload(
        {dynamic_key: dynamic_value, "reason_code": "canonical-order"}
    )

    assert normalized == {
        "http_method": "GET",
        "reason_code": "canonical-order",
    }
    assert all(type(key) is str for key in normalized)
    assert all(type(value) in {str, int} for value in normalized.values())
    assert _DynamicText.encode_calls == 0


def test_dynamic_payload_key_hash_matches_its_json_round_trip() -> None:
    event = _event()
    context = AuditTrustedContext(uuid4(), uuid4())
    normalized = _normalize_payload(
        {
            _DynamicText("http_method"): "GET",
            "reason_code": "canonical-order",
        }
    )

    assert _content_hash(event, context, normalized) == _content_hash(
        event,
        context,
        {"http_method": "GET", "reason_code": "canonical-order"},
    )


@pytest.mark.parametrize(
    "field",
    [
        "event_type",
        "component_id",
        "affected_record_type",
        "affected_record_id",
    ],
)
def test_event_text_subtypes_are_frozen_to_exact_strings(field: str) -> None:
    event = _event(**{field: _DynamicText("canonical-text")})

    assert type(getattr(event, field)) is str
    assert getattr(event, field) == "canonical-text"


@pytest.mark.parametrize(
    ("field", "expected_field"),
    [
        ("event_type", "event_type"),
        ("component_id", "component_id"),
        ("affected_record_type", "affected_record"),
        ("affected_record_id", "affected_record"),
    ],
)
def test_event_validation_uses_frozen_text_not_overridden_strip(
    field: str,
    expected_field: str,
) -> None:
    with pytest.raises(AuditEventValueRejectedError) as exc_info:
        _event(**{field: _DynamicText("   ")})

    assert exc_info.value.field == expected_field


def test_payload_validation_uses_frozen_text_not_overridden_strip() -> None:
    with pytest.raises(AuditPayloadValueRejectedError) as exc_info:
        _normalize_payload({"reason_code": _DynamicText("   ")})

    assert exc_info.value.key == "reason_code"
