from __future__ import annotations

import struct
from pathlib import Path
from typing import Final, TypeGuard

from ops.testing import isolation_candidate_records as candidate_records
from ops.testing import isolation_common as c
from ops.testing import isolation_final_input_auth as auth

from isolation_candidate_fixtures import (
    complete_empty_lineage,
    final_candidate_contract,
    final_input_fixture,
    write_final_candidate_pair,
)
from isolation_terminal_publisher_fixtures import (
    publisher_claim,
    stale_publisher_ledger,
    terminal_authorizations,
)

SHA, TREE = "a" * 40, "b" * 40
CREATED: Final = "2026-07-16T11:00:00.000000Z"
VERIFIED: Final = "2026-07-16T22:00:00.000000Z"


def freeze_fixture(root: Path) -> auth.FinalInputFreeze:
    ledger_path, _journal_path = stale_publisher_ledger(root, "reserved")
    ledger, _ = c.load_json(ledger_path)
    ledger["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    boot_observation = candidate_records.object_value(
        ledger["boot_observation"], "boot observation"
    )
    boot_observation["boot_id"] = ledger["boot_id"]
    attempt = Path(str(ledger["attempt_root"]))
    worktree = Path(str(ledger["worktree_realpath"]))
    terminal = attempt.parents[1] / "clinic-os-phase1a-final" / "terminal"
    (attempt / "claims").mkdir(mode=0o700)
    terminal.mkdir(parents=True, mode=0o700, exist_ok=True)
    files = _frozen_inputs(attempt, terminal, ledger_path, ledger)
    staging = attempt / str(only_claim(ledger)["root_relative_path"])
    app = auth.CandidateInspection(
        "sha256:" + "d" * 64, final_candidate_contract("application")
    )
    runner = auth.CandidateInspection(
        "sha256:" + "e" * 64, final_candidate_contract("browser-runner")
    )
    codex = attempt / "codex"
    uv = attempt / "uv"
    codex_raw = bytearray(64)
    codex_raw[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", codex_raw, 18, 62)
    struct.pack_into("<Q", codex_raw, 32, 64)
    struct.pack_into("<HH", codex_raw, 54, 56, 0)
    c.write_no_replace(codex, bytes(codex_raw), mode=0o555)
    c.write_no_replace(uv, b"uv\n", mode=0o555)
    suites = attempt / "final-required-browser-suites.txt"
    c.write_no_replace(
        suites,
        b"availability\npatient\nruntime-https\nscheduling\n",
        mode=c.MODE_IMMUTABLE,
    )
    return auth.FinalInputFreeze(
        *(worktree, SHA, TREE, str(ledger["attempt_id"]), attempt),
        *(files["plan"], files["proof"], files["ledger"]),
        *(files["baseline"], files["kickoff"], files["final-probe"]),
        *(files["receipts"], files["app-envelope"], files["app-history"]),
        *(files["runner-envelope"], files["runner-history"]),
        app,
        runner,
        staging,
        terminal / "inputs.json",
        auth.SourceInspection(SHA, TREE, clean=True),
        codex,
        uv,
        suites,
    )


def _frozen_inputs(
    root: Path,
    terminal: Path,
    ledger_path: Path,
    record: c.JsonObject,
) -> dict[str, Path]:
    plan = candidate_records.object_value(record["approved_plan"], "approved plan")
    proof = candidate_records.object_value(
        record["execution_host_preflight"], "execution proof"
    )
    baseline = candidate_records.object_value(record["baseline"], "baseline")
    shared = candidate_records.object_value(
        baseline["shared_evidence_manifest"], "shared baseline"
    )
    paths = {
        "plan": Path(str(plan["path"])),
        "proof": Path(str(proof["path"])),
        "baseline": Path(str(shared["path"])),
    }
    Path(str(plan["sidecar_path"])).chmod(c.MODE_IMMUTABLE)
    attempt_id = str(record["attempt_id"])
    for fixture_name, key in (
        ("kickoff_probe", "kickoff"),
        ("final_probe", "final-probe"),
    ):
        paths[key] = root / f"{key}.json"
        probe = final_input_fixture(fixture_name)
        probe["attempt_id"] = attempt_id
        probe["creation_boot_id"] = record["boot_id"]
        probe["proof_sha256"] = c.raw_sha256(paths["proof"].read_bytes())
        probe["child_path"] = (
            f"{probe['parent_path']}/clinic-os-phase1a-probe-{attempt_id}-{probe['purpose']}"
        )
        c.write_no_replace(paths[key], c.canonical_bytes(probe), mode=c.MODE_IMMUTABLE)
    receipts = root / "todo-evidence"
    receipts.mkdir(exist_ok=True)
    for todo in range(1, 21):
        path = receipts / f"task-{todo}-clinic-os-phase-1a-staff-scheduling.json"
        c.write_no_replace(
            path,
            c.canonical_bytes({"schema_version": 1, "todo": todo}),
            mode=c.MODE_IMMUTABLE,
        )
    app = write_final_candidate_pair(root, "application", attempt_id)
    runner = write_final_candidate_pair(root, "runner", attempt_id)
    complete_empty_lineage(root, attempt_id)
    record["created_at_utc"] = CREATED
    record["last_verified_at_utc"] = VERIFIED
    desired: c.JsonObject = {
        "owned_files": [],
        "published_outputs": terminal_authorizations(terminal),
    }
    publisher = publisher_claim(desired, "active")
    _publish_launcher_prefix(publisher)
    record["claims"] = [publisher]
    (root / str(publisher["root_relative_path"])).mkdir(mode=0o700)
    c.write_atomic_replace(ledger_path, c.canonical_bytes(record))
    paths.update({"ledger": ledger_path, "receipts": receipts, **app, **runner})
    return paths


def only_claim(ledger: c.JsonObject) -> c.JsonObject:
    claims = candidate_records.object_values(ledger["claims"], "claims")
    assert len(claims) == 1
    return claims[0]


def _publish_launcher_prefix(claim: c.JsonObject) -> None:
    desired = candidate_records.object_value(claim["desired"], "publisher desired")
    observed = candidate_records.object_value(claim["observed"], "publisher observed")
    authorizations = candidate_records.object_values(
        desired["published_outputs"], "publisher authorizations"
    )
    observations = candidate_records.object_values(
        observed["published_outputs"], "publisher observations"
    )
    for index, raw in enumerate(
        (b"/opt/clinic/launcher\n", b"{}\n", b"f" * 64 + b"\n")
    ):
        destination = candidate_records.authorization_destination(authorizations[index])
        c.write_no_replace(destination, raw, mode=c.MODE_IMMUTABLE)
        observations[index] = candidate_records.published_observation(
            authorizations[index], raw
        )


def is_json_value(value: object) -> TypeGuard[c.JsonValue]:
    if value is None or isinstance(value, bool | int | float | str):
        return True
    if isinstance(value, list):
        return all(is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and is_json_value(item) for key, item in value.items()
        )
    return False
