from __future__ import annotations

import copy
import hashlib
import subprocess
from pathlib import Path

import pytest
from ops.testing import isolation_stale_proof
from ops.testing.execution_host_resume_authority import (
    JOURNAL_KEYS,
    ResumePublicationInputs,
    validate_resume_publication_authority,
)
from ops.testing.isolation_common import (
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
    write_atomic_replace,
    write_no_replace,
)
from ops.testing.isolation_ledger_store import BOOT_ID_PATH
from ops.testing.isolation_snapshot_records import execution_proof_record

from isolation.isolation_stale_recovery_fixtures import (
    JOURNAL_KEYS as STALE_JOURNAL_KEYS,
)
from isolation.test_isolation_stale_resume import _coordinator, _empty_inventory
from isolation_claim_fixtures import (
    CLAIM_ID,
    claim_transitions,
    filesystem_spec,
    snapshot,
    write_spec,
)

PREVIOUS_BOOT = "11111111-1111-4111-8111-111111111111"
CRASH_STAGES = (
    "resume-proof-published",
    "resume-ledger-updated",
    "resume-boot-updated",
)


def test_committed_resume_authority_has_the_exact_outer_journal_root() -> None:
    # Given: the schema fixture and committed proof publisher authority contract.

    # When / Then: both enumerate the same closed stale-recovery fields.
    assert JOURNAL_KEYS == STALE_JOURNAL_KEYS


@pytest.mark.parametrize("crash_stage", CRASH_STAGES)
def test_default_publisher_replays_every_proof_and_boot_rebind_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_stage: str,
) -> None:
    # Given: a prior-boot proof, no current proof, and the committed publisher stub.
    ledger_path, prior_proof = _prior_boot_resume_ledger(tmp_path)
    prior_raw = prior_proof.read_bytes()
    states: list[str] = []
    _install_committed_publisher_stub(monkeypatch, states)

    def crash(stage: str, _journal: JsonObject) -> None:
        if stage == crash_stage:
            message = f"simulated committed publisher crash at {stage}"
            raise RuntimeError(message)

    # When: one durable boundary crashes and default stale recovery is replayed.
    with pytest.raises(RuntimeError, match="committed publisher crash"):
        _coordinator().reconcile_stale_boot(
            ledger_path,
            inventory_reader=_empty_inventory,
            checkpoint=crash,
        )
    completed = _coordinator().reconcile_stale_boot(
        ledger_path,
        inventory_reader=_empty_inventory,
    )

    # Then: the old proof is immutable and the current proof binds the final ledger.
    ledger, _raw = load_json(ledger_path)
    current_record = ledger["execution_host_preflight"]
    assert isinstance(current_record, dict)
    current_proof = Path(str(current_record["path"]))
    assert completed["state"] == "complete"
    assert prior_proof.read_bytes() == prior_raw
    assert current_proof != prior_proof
    assert current_proof.exists()
    assert ledger["boot_id"] == BOOT_ID_PATH.read_text().strip()
    assert states[0] == "claims-pruned"
    if crash_stage != "resume-boot-updated":
        assert "resume-proof-published" in states


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("state", "boot-updated", "authority"),
        ("failure_receipts_sha256", "f" * 64, "quiescent"),
        ("controller_recoveries", [{}], "quiescent"),
    ],
)
def test_committed_resume_authority_rejects_nonquiescent_or_late_prefixes(
    tmp_path: Path,
    field: str,
    value: JsonValue,
    message: str,
) -> None:
    # Given: a real claims-pruned resume prefix with one forbidden mutation.
    ledger_path, _prior = _prior_boot_resume_ledger(tmp_path)
    captured: list[JsonObject] = []

    def stop(stage: str, journal: JsonObject) -> None:
        if stage == "claims-pruned":
            captured.append(copy.deepcopy(journal))
            error = "captured claims-pruned authority"
            raise RuntimeError(error)

    with pytest.raises(RuntimeError, match="captured claims-pruned"):
        _coordinator().reconcile_stale_boot(
            ledger_path,
            inventory_reader=_empty_inventory,
            checkpoint=stop,
        )
    journal = captured[0]
    journal[field] = value
    ledger, ledger_raw = load_json(ledger_path)
    authority_root = ledger_path.parent.parent
    proof = authority_root / (
        f"clinic-os-phase1a-execution-host-{journal['current_boot_id']}.json"
    )

    # When / Then: the committed predicate refuses the widened authority.
    with pytest.raises(ValueError, match=message):
        validate_resume_publication_authority(
            ResumePublicationInputs(
                journal,
                canonical_bytes(journal),
                Path(str(ledger["attempt_root"])) / "stale-boot-recovery.json",
                ledger,
                ledger_raw,
                ledger_path,
                authority_root,
                str(journal["current_boot_id"]),
                proof,
            )
        )


def _prior_boot_resume_ledger(tmp_path: Path) -> tuple[Path, Path]:
    ledger_path = snapshot(tmp_path)
    claim_transitions().reserve_claim(
        ledger_path,
        write_spec(tmp_path, filesystem_spec(CLAIM_ID, [])),
    )
    ledger, _raw = load_json(ledger_path)
    ledger["boot_id"] = PREVIOUS_BOOT
    observation = ledger["boot_observation"]
    assert isinstance(observation, dict)
    observation["boot_id"] = PREVIOUS_BOOT
    authority_root = ledger_path.parent.parent
    current_record = ledger["execution_host_preflight"]
    assert isinstance(current_record, dict)
    current = Path(str(current_record["path"]))
    proof, _raw = load_json(current)
    proof["boot_id"] = PREVIOUS_BOOT
    prior = authority_root / f"clinic-os-phase1a-execution-host-{PREVIOUS_BOOT}.json"
    write_no_replace(prior, canonical_bytes(proof), mode=0o400)
    current.unlink()
    binding = ledger["authority_binding"]
    assert isinstance(binding, dict)
    prior_record = execution_proof_record(
        prior,
        Path(str(binding["authority_workspace_realpath"])),
        authority_root,
        str(ledger["foundation_sha"]),
        PREVIOUS_BOOT,
    )
    ledger["execution_host_preflight"] = prior_record
    probe_path = Path(str(ledger["attempt_root"])) / (
        "execution-host-probes/todo1-kickoff.json"
    )
    probe, _raw = load_json(probe_path)
    probe["creation_boot_id"] = PREVIOUS_BOOT
    probe["proof_sha256"] = prior_record["sha256"]
    probe_path.chmod(0o600)
    write_atomic_replace(probe_path, canonical_bytes(probe))
    probe_path.chmod(0o400)
    write_atomic_replace(ledger_path, canonical_bytes(ledger))
    return ledger_path, prior


def _install_committed_publisher_stub(
    monkeypatch: pytest.MonkeyPatch,
    states: list[str],
) -> None:
    def run(
        command: tuple[object, ...],
        **_options: object,
    ) -> subprocess.CompletedProcess[str]:
        assert command[1] == isolation_stale_proof.PUBLISHER
        assert command[2:4] == ("publish", "--authority-root")
        assert command[5] == "--stale-boot-resume"
        assert command[7] == "--stable-lock-fd"
        authority_root = Path(str(command[4]))
        journal_path = Path(str(command[6]))
        ledger_path = authority_root / "evidence/isolation-ledger-phase1a.json"
        journal, journal_raw = load_json(journal_path)
        ledger, ledger_raw = load_json(ledger_path)
        boot_id = str(journal["current_boot_id"])
        proof_path = authority_root / f"clinic-os-phase1a-execution-host-{boot_id}.json"
        if not proof_path.exists():
            prior_record = ledger["execution_host_preflight"]
            assert isinstance(prior_record, dict)
            prior = Path(str(prior_record["path"]))
            proof, _raw = load_json(prior)
            proof["boot_id"] = boot_id
            write_no_replace(proof_path, canonical_bytes(proof), mode=0o400)
        validate_resume_publication_authority(
            ResumePublicationInputs(
                journal,
                journal_raw,
                journal_path,
                ledger,
                ledger_raw,
                ledger_path,
                authority_root,
                boot_id,
                proof_path,
            )
        )
        states.append(str(journal["state"]))
        digest = hashlib.sha256(proof_path.read_bytes()).hexdigest()
        rendered = tuple(str(item) for item in command)
        return subprocess.CompletedProcess(rendered, 0, f"{digest}\n", "")

    monkeypatch.setattr(subprocess, "run", run)
