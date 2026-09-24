from __future__ import annotations

import copy
import importlib
from pathlib import Path
from typing import TYPE_CHECKING, cast

import rfc8785
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    JsonObject,
    canonical_bytes,
    canonical_sha256,
    load_json,
    raw_sha256,
    write_atomic_replace,
    write_no_replace,
)

if TYPE_CHECKING:
    import pytest

    from isolation.isolation_user_fixtures import UserGateFixture

REBOOT_ID = "22222222-2222-4222-8222-222222222222"


def reboot_user_gate(
    fixture: UserGateFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    boot_path = tmp_path / "current-boot-id"
    boot_path.write_text(f"{REBOOT_ID}\n")
    for module_name in (
        "ops.testing.isolation_ledger_store",
        "ops.testing.isolation_terminal_revalidation",
        "ops.testing.isolation_user_fix",
    ):
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, "BOOT_ID_PATH", boot_path)
    ledger, prior_raw = load_json(fixture.ledger_path)
    previous_boot = str(ledger["boot_id"])
    observation = copy.deepcopy(cast("JsonObject", ledger["boot_observation"]))
    observation["boot_id"] = REBOOT_ID
    observation["observed_at_utc"] = "2026-07-17T00:00:00.000001Z"
    ledger["boot_id"] = REBOOT_ID
    ledger["boot_observation"] = observation
    ledger["last_verified_at_utc"] = observation["observed_at_utc"]
    ledger_raw = canonical_bytes(ledger)
    write_atomic_replace(fixture.ledger_path, ledger_raw)
    baseline = cast("JsonObject", ledger["baseline"])
    shared = cast("JsonObject", baseline["shared_evidence_manifest"])
    artifact: JsonObject = {
        "accepted_close_prepared_sha256": None,
        "approvals_sha256": fixture.approvals_sha256,
        "attempt_id": ledger["attempt_id"],
        "boot_changed": True,
        "boot_observation": observation,
        "boot_observation_sha256": raw_sha256(rfc8785.dumps(observation)),
        "current_boot_id": REBOOT_ID,
        "final_relative_path": "clinic-os-phase1a-final/final.json",
        "final_sha256": fixture.final_sha256,
        "lock_identity_sha256": canonical_sha256(ledger["lock_identity"]),
        "post_update_ledger_sha256": raw_sha256(ledger_raw),
        "pre_update_ledger_sha256": raw_sha256(prior_raw),
        "previous_boot_id": previous_boot,
        "publisher_final_journal_sha256": fixture.publisher_journal_sha256,
        "purpose": "terminal-final",
        "reboot_stable_baseline_sha256": ledger["reboot_stable_baseline_sha256"],
        "schema_version": 1,
        "sha": load_json(fixture.control_root / "final.json")[0]["sha"],
        "shared_evidence_manifest_sha256": shared["sha256"],
        "tree_sha": fixture.tree_sha,
        "verified_at_utc": observation["observed_at_utc"],
    }
    root = Path(str(ledger["attempt_root"])) / "terminal-revalidation"
    path = root / f"{REBOOT_ID}.json"
    write_no_replace(path, canonical_bytes(artifact), mode=MODE_IMMUTABLE)
    return path
