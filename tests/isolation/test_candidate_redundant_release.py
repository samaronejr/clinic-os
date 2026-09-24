from __future__ import annotations

from pathlib import Path

from ops.testing.isolation_common import load_json

from isolation.isolation_candidate_fixtures import reserve_and_activate_candidate
from isolation_claim_fixtures import CLAIM_ID, claim_transitions, snapshot


def test_redundant_unbound_candidate_releases_without_touching_destination(
    tmp_path: Path,
) -> None:
    ledger_path = snapshot(tmp_path)
    spec = reserve_and_activate_candidate(tmp_path, ledger_path)
    ledger, _ = load_json(ledger_path)
    desired = spec["desired"]
    assert isinstance(desired, dict)
    outputs = desired["published_outputs"]
    assert isinstance(outputs, list)
    envelope_output = outputs[0]
    assert isinstance(envelope_output, dict)
    root_path = envelope_output["root_path"]
    relative_paths = envelope_output["relative_paths"]
    assert isinstance(root_path, str)
    assert isinstance(relative_paths, list)
    assert len(relative_paths) == 1
    relative_path = relative_paths[0]
    assert isinstance(relative_path, str)
    destination = Path(root_path) / relative_path
    destination.parent.mkdir(parents=True)
    _ = destination.write_bytes(b"previously-published-candidate\n")
    destination.chmod(0o400)
    original = destination.read_bytes()
    claim_root = Path(str(ledger["attempt_root"])) / "claims" / CLAIM_ID
    claim_root.rmdir()

    claim_transitions().release_claim(ledger_path, CLAIM_ID)

    released, _ = load_json(ledger_path)
    assert released["claims"] == []
    assert destination.read_bytes() == original
