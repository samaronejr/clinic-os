from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from isolation_stale_recovery_fixtures import JOURNAL_KEYS

PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]


def test_stale_recovery_schema_closes_root_actions_and_recoveries() -> None:
    # Given: the normative changed-boot recovery schema and exact journal fields.
    schema_path = PROJECT_ROOT / "ops" / "testing" / "stale-boot-recovery.schema.json"

    # When: all locally declared object schemas and action mappings are inspected.
    schema = json.loads(schema_path.read_text())
    objects = _object_schemas(schema)
    action = schema["$defs"]["resourceAction"]
    mapping_text = json.dumps(action["allOf"], sort_keys=True)

    # Then: root/nested objects are closed and every physical mapping is fixed.
    assert set(schema["properties"]) == JOURNAL_KEYS
    assert set(schema["required"]) == JOURNAL_KEYS
    assert objects
    assert all(item.get("additionalProperties") is False for item in objects)
    assert set(action["properties"]) == {
        "action_id",
        "claim_id",
        "identity_sha256",
        "operation",
        "resource_kind",
    }
    for operation in (
        "observe-prior-boot-process-absent",
        "stop-remove-container",
        "detach-borrowed-network",
        "remove-owned-network",
        "remove-owned-volume",
        "remove-staging",
        "publish-or-adopt-candidate-envelope",
        "publish-or-adopt-candidate-history",
    ):
        assert operation in mapping_text


def test_stale_recovery_schema_closes_states_and_controller_fields() -> None:
    # Given: the state machine and controller-recovery definitions.
    schema_path = PROJECT_ROOT / "ops" / "testing" / "stale-boot-recovery.schema.json"
    schema = json.loads(schema_path.read_text())
    controller = schema["$defs"]["controllerRecovery"]

    # When / Then: state enums and the immutable controller record are exact.
    assert schema["properties"]["state"]["enum"] == [
        "prepared",
        "cleaning",
        "resources-absent",
        "claims-prune-intent",
        "claims-pruned",
        "resume-proof-published",
        "receipts-finalizing",
        "receipts-finalized",
        "publisher-release-intent",
        "boot-updated",
        "publisher-released",
        "complete",
    ]
    assert set(controller["properties"]) == {
        "common_receipt_path",
        "common_receipt_sha256",
        "controller_kind",
        "final_journal_sha256",
        "initial_journal_sha256",
        "journal_path",
        "lane_or_form",
        "lease_path",
        "state",
    }


def _object_schemas(value: object) -> list[dict[str, object]]:
    if isinstance(value, list):
        return [item for entry in value for item in _object_schemas(entry)]
    if not isinstance(value, dict):
        return []
    current = [value] if value.get("type") == "object" else []
    return current + [
        item for entry in value.values() for item in _object_schemas(entry)
    ]
