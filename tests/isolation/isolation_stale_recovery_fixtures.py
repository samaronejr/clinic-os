from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject, JsonValue

ATTEMPT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PREVIOUS_BOOT = "11111111-1111-4111-8111-111111111111"
CURRENT_BOOT = "22222222-2222-4222-8222-222222222222"
TIMESTAMP = "2026-07-16T22:00:00.000001Z"
JOURNAL_KEYS = {
    "schema_version",
    "attempt_id",
    "previous_boot_id",
    "current_boot_id",
    "recovery_goal",
    "state",
    "prior_ledger_sha256",
    "resource_actions",
    "runner_recoveries",
    "completed_action_ids",
    "reboot_stable_baseline_sha256",
    "boot_observation",
    "boot_observation_sha256",
    "shared_evidence_manifest_sha256",
    "lock_identity_sha256",
    "post_cleanup_ledger_sha256",
    "post_update_ledger_sha256",
    "resume_execution_host_preflight_path",
    "resume_execution_host_preflight_sha256",
    "f3_recovery_required",
    "f3_journal_path",
    "f3_initial_journal_sha256",
    "f3_final_journal_sha256",
    "f3_receipt_path",
    "f3_receipt_sha256",
    "controller_recoveries",
    "publisher_claim_id",
    "publisher_final_wave_journal_path",
    "publisher_initial_journal_sha256",
    "publisher_release_intent_sha256",
    "publisher_final_journal_sha256",
    "failure_receipts_sha256",
    "updated_at_utc",
}


def stale_claim(
    claim_id: str,
    kind: str,
    dependencies: list[JsonValue],
    *,
    purpose: str,
) -> JsonObject:
    observed: JsonObject
    if kind == "process":
        observed = {
            "listener_socket_inode": None,
            "listeners": [],
            "members": [
                {
                    "argv_sha256": "a" * 64,
                    "executable_realpath": "/usr/bin/python3",
                    "gid": 1000,
                    "pgid": 123,
                    "pid": 123,
                    "ppid": 1,
                    "sid": 123,
                    "start_ticks": 456,
                    "uid": 1000,
                }
            ],
        }
    else:
        observed = {"owned_files": [], "published_outputs": []}
    return {
        "activated_at_utc": "2026-07-16T21:00:00.000002Z",
        "candidate_envelope_binding": None,
        "claim_id": claim_id,
        "dependency_claim_ids": dependencies,
        "desired": {},
        "kind": kind,
        "last_verified_at_utc": "2026-07-16T21:00:00.000003Z",
        "observed": observed,
        "prepared_at_utc": None,
        "purpose": purpose,
        "reserved_at_utc": "2026-07-16T21:00:00.000001Z",
        "root_relative_path": f"claims/{claim_id}",
        "runner_creation": None,
        "status": "active",
    }


def stale_ledger(claims: list[JsonObject]) -> JsonObject:
    return {
        "attempt_id": ATTEMPT_ID,
        "attempt_root": f"/evidence/runtime/{ATTEMPT_ID}",
        "baseline": {
            "containers": [],
            "listeners": [],
            "networks": [],
            "shared_evidence_manifest": {
                "entry_count": 0,
                "path": f"/evidence/runtime/{ATTEMPT_ID}/shared.json",
                "sha256": "b" * 64,
            },
            "volumes": [],
        },
        "boot_id": PREVIOUS_BOOT,
        "claims": cast("JsonValue", claims),
        "lock_identity": {
            "device": 1,
            "gid": 1000,
            "inode": 2,
            "link_count": 1,
            "mode": 0o600,
            "uid": 1000,
        },
        "reboot_stable_baseline_sha256": "c" * 64,
    }


def boot_observation() -> JsonObject:
    return {
        "boot_id": CURRENT_BOOT,
        "containers": [],
        "listeners": [],
        "networks": [],
        "observed_at_utc": TIMESTAMP,
        "volumes": [],
    }
