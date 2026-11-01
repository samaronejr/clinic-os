"""Digest differentials use complete admitted envelopes and independent fields."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from apps.ai.action_contracts import ActionInputError
from apps.ai.action_payload import parse_payload

from ai.test_action_payload import payload_json

if TYPE_CHECKING:
    from apps.ai.action_payload import JSON


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("organization_id", "20000000-0000-4000-8000-000000000002"),
        ("clinic_id", "20000000-0000-4000-8000-000000000003"),
        ("patient_id", "20000000-0000-4000-8000-000000000004"),
        (
            "subject",
            {
                "kind": "workflow_definition",
                "id": "20000000-0000-4000-8000-000000000005",
                "expected_state": "published",
                "revision": 2,
            },
        ),
        (
            "source_versions",
            [
                {
                    "kind": "workflow_definition",
                    "id": "20000000-0000-4000-8000-000000000006",
                    "version": 1,
                    "revision": 2,
                    "content_sha256": "a" * 64,
                }
            ],
        ),
        ("document", {"version": 2, "sha256": "a" * 64}),
        (
            "recipients",
            [
                {
                    "kind": "synthetic",
                    "id": "20000000-0000-4000-8000-000000000007",
                    "destination": "synthetic.invalid",
                    "version": 1,
                    "consent_refs": [],
                    "delegation_refs": [],
                }
            ],
        ),
        (
            "amounts",
            {
                "currency": "BRL",
                "centavos": "1",
                "direction": "debit",
                "allocations": [],
            },
        ),
        (
            "provider",
            {
                "capability_version": "20000000-0000-4000-8000-000000000008",
                "account": "synthetic-alternative",
                "environment": "synthetic",
                "api_version": "v2",
            },
        ),
        ("not_before", "2030-12-31T23:00:00Z"),
        ("expires_at", "2031-01-01T02:00:00Z"),
        (
            "approver_authority_snapshots",
            [
                {
                    "user_id": "20000000-0000-4000-8000-000000000009",
                    "permission": "tasks.assign",
                    "memberships": [
                        {
                            "id": "20000000-0000-4000-8000-000000000010",
                            "role": "owner",
                            "bundle_version": 2,
                        }
                    ],
                }
            ],
        ),
        (
            "approval_policy",
            {
                "policy_id": "20000000-0000-4000-8000-000000000011",
                "version": 2,
                "quorum": 2,
                "autonomy": "A3",
            },
        ),
        ("compensation_policy", {"mode": "none", "version": 1}),
        ("idempotency_key", "20000000-0000-4000-8000-000000000012"),
    ],
)
def test_admitted_field_change_changes_the_digest(field: str, value: JSON) -> None:
    # Given two complete envelopes that differ in one independently listed field.
    data = payload_json()
    first = parse_payload(json.dumps(data))
    data[field] = value

    # When the changed envelope is admitted, not refused by its schema.
    second = parse_payload(json.dumps(data))

    # Then hashing must distinguish the changed bound value.
    assert first.data()[field] != second.data()[field]
    assert first.digest != second.digest


def test_admitted_task_deadline_change_changes_the_digest() -> None:
    # Given the registered task schema, whose due_at has multiple valid values.
    data = payload_json()
    data["operation"] = {"name": "workflows.review_task", "version": 1}
    data["parameters"] = {"due_at": "2031-01-02T00:00:00Z"}
    first = parse_payload(json.dumps(data))

    # When only the valid deadline changes.
    data["parameters"] = {"due_at": "2031-01-03T00:00:00Z"}
    second = parse_payload(json.dumps(data))

    # Then the exact task effect has a different digest.
    assert first.digest != second.digest


def test_admitted_operation_and_its_closed_arguments_change_the_digest() -> None:
    # Given the reminder operation and its admitted closed argument schema.
    data = payload_json()
    first = parse_payload(json.dumps(data))

    # When switching operations coherently requires its different argument schema.
    data["operation"] = {"name": "workflows.review_task", "version": 1}
    data["parameters"] = {"due_at": "2031-01-02T00:00:00Z"}
    second = parse_payload(json.dumps(data))

    # Then the admitted operation/argument pair cannot reuse the old digest.
    assert first.digest != second.digest


def test_reordered_mixed_authority_is_rejected_without_silent_sorting() -> None:
    # Given a valid canonical mixed-bundle authority array.
    data = payload_json()
    memberships = [
        {
            "id": "20000000-0000-4000-8000-000000000002",
            "role": "org_admin",
            "bundle_version": 1,
        },
        {
            "id": "10000000-0000-4000-8000-000000000001",
            "role": "clinic_admin",
            "bundle_version": 2,
        },
    ]
    data["approver_authority_snapshots"] = [
        {
            "user_id": "20000000-0000-4000-8000-000000000003",
            "permission": "tasks.assign",
            "memberships": memberships,
        }
    ]
    admitted = parse_payload(json.dumps(data))

    # When only the order changes, the same members cannot be silently normalized.
    memberships.reverse()
    with pytest.raises(ActionInputError):
        parse_payload(json.dumps(data))

    # The accepted bytes retain their original canonical order.
    assert (
        json.loads(admitted.canonical)["approver_authority_snapshots"][0][
            "memberships"
        ][0]["bundle_version"]
        == 1
    )
