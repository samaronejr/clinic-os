from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Final, cast

import rfc8785

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject, JsonValue

ATTEMPT_ID: Final = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CLAIM_ID: Final = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
REVISION: Final = "1" * 40
TREE: Final = "2" * 40
ATTEMPT_ROOT: Final = "/clinic-fixtures/phase1a-stale-vector"
EXPECTED_APPLICATION_HASHES: Final = (
    "782aa0e525ffeb06688c72ad6328095fc3c3fd1f137b876209b29e38e62a548a",
    "84ef60ddcbc13afc78cab5d2ab4b42be1df3e76dc2f5706ca67575e21215cb87",
    "a461189756aec21248fa6d07091cc1366731d2ab5e508dae5cb323b36e153f86",
)
EXPECTED_RUNNER_HASHES: Final = (
    "53246a3a28cdaff2e22287382ec0e38a604362eae3e75b28ee214b06a09ca13e",
    "4b084a9ea556941ad5e9a46988558e2c79f55e9739c3017fd80a76a4734d2e91",
    "ca62d617c4ac2afe83d62a68e91c9715a9be27862ccd711b225736e0533a32c8",
)


def fixed_candidate_state(*, runner: bool) -> tuple[JsonObject, JsonObject]:
    prefix = "candidate-browser-runner" if runner else "candidate-application"
    kind = "browser-runner" if runner else "application"
    filename = "browser-runner-envelope.json" if runner else "application-envelope.json"
    suites = (
        ["availability", "patient", "runtime-https", "scheduling"] if runner else []
    )
    envelope: JsonObject = {
        "attempt_id": ATTEMPT_ID,
        "authorization_id": f"{prefix}-envelope",
        "claim_id": CLAIM_ID,
        "image_contract": {
            "available_suite_ids": cast("JsonValue", suites),
            "kind": kind,
            "revision_sha": REVISION,
            "source_entry_count": 17,
            "source_manifest_sha256": "4" * 64,
            "tree_sha": TREE,
        },
        "image_id": "sha256:" + ("3" * 64),
        "published_at_utc": "2026-07-16T12:00:00.000003Z",
        "revision_sha": REVISION,
        "schema_version": 1,
        "tree_sha": TREE,
    }
    binding: JsonObject = {
        "envelope": envelope,
        "envelope_sha256": hashlib.sha256(rfc8785.dumps(envelope) + b"\n").hexdigest(),
        "schema_version": 1,
    }
    authorizations = _authorizations(prefix, filename)
    observations = [
        {
            "authorization_id": item["authorization_id"],
            "entries": [],
            "governing_lock": "stable",
            "output_kind": item["output_kind"],
            "root_path": item["root_path"],
            "status": "unpublished",
        }
        for item in authorizations
    ]
    claim: JsonObject = {
        "activated_at_utc": "2026-07-16T12:00:00.000002Z",
        "candidate_envelope_binding": binding,
        "claim_id": CLAIM_ID,
        "dependency_claim_ids": [],
        "desired": {
            "owned_files": [],
            "published_outputs": cast("JsonValue", authorizations),
        },
        "kind": "filesystem",
        "last_verified_at_utc": "2026-07-16T12:00:00.000003Z",
        "observed": {
            "owned_files": [],
            "published_outputs": cast("JsonValue", observations),
        },
        "prepared_at_utc": None,
        "purpose": f"{prefix}-publisher",
        "reserved_at_utc": "2026-07-16T12:00:00.000001Z",
        "root_relative_path": f"claims/{CLAIM_ID}",
        "runner_creation": None,
        "status": "active",
    }
    ledger: JsonObject = {"attempt_id": ATTEMPT_ID, "attempt_root": ATTEMPT_ROOT}
    return ledger, claim


def _authorizations(prefix: str, filename: str) -> list[JsonObject]:
    return [
        {
            "authorization_id": f"{prefix}-envelope",
            "gid": 1000,
            "governing_lock": "stable",
            "mode": 0o400,
            "output_kind": "candidate-image",
            "predecessor_authorization_ids": [],
            "relative_paths": [f"{REVISION}/{filename}"],
            "root_path": f"{ATTEMPT_ROOT}/candidate-images",
            "uid": 1000,
        },
        {
            "authorization_id": f"{prefix}-publication-history",
            "gid": 1000,
            "governing_lock": "stable",
            "mode": 0o400,
            "output_kind": "publication-history",
            "predecessor_authorization_ids": [f"{prefix}-envelope"],
            "relative_paths": [f"{CLAIM_ID}.json"],
            "root_path": f"{ATTEMPT_ROOT}/publication-history",
            "uid": 1000,
        },
    ]
