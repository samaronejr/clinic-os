"""The approval digest binds the closed wire format, not Python coercions."""

import hashlib
import json
from copy import deepcopy

import pytest
from apps.ai.action_contracts import ActionInputError
from apps.ai.action_payload import parse_payload


def payload_json() -> dict[str, object]:
    return {
        "schema_version": 1,
        "action_id": "10000000-0000-4000-8000-000000000001",
        "operation": {"name": "comms.synthetic_reminder", "version": 1},
        "organization_id": "10000000-0000-4000-8000-000000000002",
        "clinic_id": "10000000-0000-4000-8000-000000000003",
        "patient_id": None,
        "subject": {
            "kind": "workflow_definition",
            "id": "10000000-0000-4000-8000-000000000004",
            "expected_state": "published",
            "revision": 1,
        },
        "source_versions": [],
        "document": None,
        "recipients": [],
        "amounts": {
            "currency": "BRL",
            "centavos": "0",
            "direction": "none",
            "allocations": [],
        },
        "provider": {
            "capability_version": "10000000-0000-4000-8000-000000000005",
            "account": "synthetic",
            "environment": "synthetic",
            "api_version": "v1",
        },
        "parameters": {"template": "logistics-v1"},
        "not_before": "2031-01-01T00:00:00Z",
        "expires_at": "2031-01-01T01:00:00Z",
        "approver_authority_snapshots": [],
        "approval_policy": {
            "policy_id": "10000000-0000-4000-8000-000000000006",
            "version": 1,
            "quorum": 1,
            "autonomy": "A3",
        },
        "compensation_policy": {"mode": "manual", "version": 1},
        "idempotency_key": "10000000-0000-4000-8000-000000000007",
    }


def test_canonicalization_ignores_object_order_and_json_escapes() -> None:
    given = payload_json()
    first = parse_payload(json.dumps(given))
    second = parse_payload(json.dumps(dict(reversed(list(given.items())))))
    assert first.canonical == second.canonical
    assert first.digest == hashlib.sha256(first.canonical).hexdigest()


@pytest.mark.parametrize(
    ("field", "duplicate"),
    [
        (
            '"schema_version":1',
            '"schema_version":1,"schema_version":1',
        ),
        (
            '"template":"logistics-v1"',
            '"template":"logistics-v1","template":"logistics-v1"',
        ),
    ],
    ids=["top-level", "nested"],
)
def test_complete_envelope_refuses_duplicate_keys(field: str, duplicate: str) -> None:
    # Given an otherwise admitted envelope, including every required field.
    raw = json.dumps(payload_json(), separators=(",", ":"))
    assert parse_payload(raw).data()["schema_version"] == 1
    duplicated = raw.replace(field, duplicate, 1)
    assert duplicated != raw

    # When a last-value-wins decoder would otherwise retain valid field values.
    with pytest.raises(ActionInputError):
        parse_payload(duplicated)


@pytest.mark.parametrize("number", ["NaN", "Infinity", "1.0", "1e0", "-0.0"])
def test_complete_envelope_refuses_noninteger_json_numbers(number: str) -> None:
    # Given a complete positive control, not a missing-field rejection.
    raw = json.dumps(payload_json(), separators=(",", ":"))
    assert parse_payload(raw).data()["schema_version"] == 1

    # When the wire representation violates the integer-only action contract.
    changed = raw.replace('"schema_version":1', f'"schema_version":{number}', 1)
    with pytest.raises(ActionInputError):
        parse_payload(changed)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unexpected", True),
        ("schema_version", True),
        ("schema_version", 2),
        ("not_before", "2031-01-01"),
        ("expires_at", "2031-01-01T00:00:00Z"),
        ("operation", {"name": "shell", "version": 1}),
        ("parameters", {"url": "https://synthetic.invalid/"}),
        (
            "amounts",
            {
                "currency": "BRL",
                "centavos": "01",
                "direction": "none",
                "allocations": [],
            },
        ),
    ],
)
def test_closed_fields_and_operation_arguments_are_enforced(
    field: str,
    value: object,
) -> None:
    given = deepcopy(payload_json())
    given[field] = value
    with pytest.raises(ActionInputError):
        parse_payload(json.dumps(given))


def test_unicode_is_never_normalized_inside_bound_text() -> None:
    first = payload_json()
    first["document"] = {"version": 1, "sha256": "a" * 64}
    first["parameters"] = {"template": "logistics-v1"}
    first["recipients"] = [
        {
            "kind": "synthetic",
            "id": "10000000-0000-4000-8000-000000000008",
            "destination": "é",
            "version": 1,
            "consent_refs": [],
            "delegation_refs": [],
        }
    ]
    second = json.dumps(first).replace("\\u00e9", "e\\u0301")
    assert parse_payload(json.dumps(first)).digest != parse_payload(second).digest
