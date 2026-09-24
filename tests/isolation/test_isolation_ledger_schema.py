from __future__ import annotations

import json
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]


def test_ledger_schema_closes_every_object_and_the_exact_root() -> None:
    # Given: the normative schema-v2 path and exact root field contract.
    schema_path = PROJECT_ROOT / "ops" / "testing" / "isolation-ledger.schema.json"
    expected_root = {
        "approved_plan",
        "attempt_id",
        "attempt_root",
        "authority_binding",
        "baseline",
        "boot_id",
        "boot_observation",
        "claims",
        "closed_at_utc",
        "created_at_utc",
        "execution_host_preflight",
        "foundation_sha",
        "last_verified_at_utc",
        "lock_identity",
        "lock_path",
        "reboot_stable_baseline_sha256",
        "rejection_close",
        "schema_version",
        "state",
        "worktree_realpath",
    }

    # When: every object definition is inspected recursively.
    schema = json.loads(schema_path.read_text())
    objects = _object_schemas(schema)

    # Then: no object is open-ended and the root admits no alias or extra field.
    assert objects
    assert all(item.get("additionalProperties") is False for item in objects)
    assert set(schema["properties"]) == expected_root


def test_ledger_schema_closes_filesystem_desired_and_observed_claims() -> None:
    # Given: the schema-v2 contract for mutable staging and immutable outputs.
    schema_path = PROJECT_ROOT / "ops" / "testing" / "isolation-ledger.schema.json"
    expected_definitions = {
        "filesystemDesired",
        "filesystemObserved",
        "ownedFileDesired",
        "ownedFileObserved",
        "publishedOutputAuthorization",
        "publishedOutputObservation",
    }

    # When: the claim-kind definitions and discriminator matrix are inspected.
    schema = json.loads(schema_path.read_text())
    definitions = schema["$defs"]

    # Then: filesystem desired/observed objects have the exact closed field sets.
    assert expected_definitions <= set(definitions)
    assert set(definitions["filesystemDesired"]["properties"]) == {
        "owned_files",
        "published_outputs",
    }
    assert set(definitions["filesystemObserved"]["properties"]) == {
        "owned_files",
        "published_outputs",
    }
    assert definitions["claim"]["allOf"]


def test_ledger_schema_closes_stack_desired_and_observed_claims() -> None:
    # Given: the schema-v2 contract for reserved and active stack resources.
    schema_path = PROJECT_ROOT / "ops" / "testing" / "isolation-ledger.schema.json"
    expected_definitions = {
        "environmentContract",
        "stackDesired",
        "stackObserved",
        "stackServiceDesired",
        "stackServiceObserved",
    }

    # When: the stack discriminator definitions are inspected.
    definitions = json.loads(schema_path.read_text())["$defs"]

    # Then: the desired and observed stack roots expose only normative fields.
    assert expected_definitions <= set(definitions)
    assert set(definitions["stackDesired"]["properties"]) == {
        "borrowed_network_refs",
        "borrowed_volume_refs",
        "database_names",
        "loopback_ports",
        "owned_networks",
        "owned_volumes",
        "project",
        "services",
    }
    assert set(definitions["stackObserved"]["properties"]) == {
        "borrowed_networks",
        "borrowed_volumes",
        "container_ids",
        "listeners",
        "owned_networks",
        "owned_volumes",
        "services",
    }


def test_ledger_schema_closes_process_desired_and_observed_claims() -> None:
    # Given: the schema-v2 contract for reserved and active host processes.
    schema_path = PROJECT_ROOT / "ops" / "testing" / "isolation-ledger.schema.json"
    expected_definitions = {
        "borrowedFileRef",
        "pathLstat",
        "processDesired",
        "processMember",
        "processObserved",
    }

    # When: the process discriminator definitions are inspected.
    definitions = json.loads(schema_path.read_text())["$defs"]

    # Then: process desired and observed roots expose only normative fields.
    assert expected_definitions <= set(definitions)
    assert set(definitions["processDesired"]["properties"]) == {
        "argv",
        "argv_sha256",
        "borrowed_file_refs",
        "environment_contract",
        "gid",
        "gunicorn_config_lstat",
        "gunicorn_config_path",
        "gunicorn_config_sha256",
        "host_ports",
        "interpreter_realpath",
        "interpreter_sha256",
        "launcher_lstat",
        "launcher_path",
        "module",
        "process_model",
        "uid",
        "worker_count",
    }
    assert set(definitions["processObserved"]["properties"]) == {
        "listener_socket_inode",
        "listeners",
        "members",
    }


def test_ledger_schema_closes_runner_creation_and_claim_state_matrix() -> None:
    # Given: the schema-v2 runner write-ahead and claim lifecycle contract.
    schema_path = PROJECT_ROOT / "ops" / "testing" / "isolation-ledger.schema.json"

    # When: runner and claim conditional matrices are inspected.
    definitions = json.loads(schema_path.read_text())["$defs"]
    runner_matrix = definitions["runnerCreation"]["allOf"]
    claim_matrix = definitions["claim"]["allOf"]
    runner_text = json.dumps(runner_matrix, sort_keys=True)
    claim_text = json.dumps(claim_matrix, sort_keys=True)

    # Then: every append-only runner state and prepared claim status is closed.
    assert len(runner_matrix) == 4
    for state in ("intent", "prepared", "remove-intent", "removed"):
        assert f'"const": "{state}"' in runner_text
    assert '"const": "prepared"' in claim_text
    assert '"runner_creation"' in claim_text


def test_ledger_schema_closes_candidate_envelope_binding() -> None:
    # Given: the candidate envelope is durably embedded before publication.
    schema_path = PROJECT_ROOT / "ops" / "testing" / "isolation-ledger.schema.json"

    # When: the candidate envelope and one-way binding definitions are inspected.
    definitions = json.loads(schema_path.read_text())["$defs"]
    envelope = definitions["candidateEnvelope"]
    binding = definitions["candidateEnvelopeBinding"]

    # Then: no legacy authorization hashes or open envelope fields are admitted.
    assert set(envelope["properties"]) == {
        "attempt_id",
        "authorization_id",
        "claim_id",
        "image_contract",
        "image_id",
        "published_at_utc",
        "revision_sha",
        "schema_version",
        "tree_sha",
    }
    assert set(binding["properties"]) == {
        "envelope",
        "envelope_sha256",
        "schema_version",
    }
    assert binding["properties"]["envelope"]["$ref"] == "#/$defs/candidateEnvelope"


def _object_schemas(value: object) -> list[dict[str, object]]:
    if isinstance(value, list):
        return [item for entry in value for item in _object_schemas(entry)]
    if not isinstance(value, dict):
        return []
    current = [value] if value.get("type") == "object" else []
    return current + [
        item for entry in value.values() for item in _object_schemas(entry)
    ]
